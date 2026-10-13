// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EventList, EventRow, HistoryTurn, ThreadTurn } from './ChatMessages';
import type { AgentEvent, AgentThreadMessage, GoalTurn } from './types';

// The round marker is rendered directly: what it says about a report is the
// whole subject, and mounting a conversation would bury it under a transcript.

const ROUND: AgentThreadMessage = {
  id: 1,
  role: 'user',
  content: 'Continue working toward the goal.',
  jobId: 'ajob_2',
  createdAt: null,
};

function round(goalTurn: GoalTurn) {
  return render(<ThreadTurn message={ROUND} goalTurn={goalTurn} />);
}

afterEach(cleanup);

describe('reasoning in the transcript', () => {
  it('starts collapsed, renders Markdown, and keeps reply actions on the answer', () => {
    const onFork = vi.fn();
    render(
      <>
        <EventRow
          event={{ kind: 'thinking', text: 'Inspect **the parser** first.', sourceEventId: 1 }}
          blockKey="ajob_1:event:1"
          onFork={onFork}
        />
        <EventRow event={{ kind: 'message', text: 'The parser is fixed.' }} onFork={onFork} />
      </>,
    );

    const reasoning = screen.getByLabelText('Agent reasoning');
    expect(reasoning.tagName).toBe('DETAILS');
    expect(reasoning).toHaveAttribute('data-block-key', 'ajob_1:event:1');
    expect(reasoning).not.toHaveAttribute('open');
    expect(within(reasoning).getByText('the parser').tagName).toBe('STRONG');
    expect(within(reasoning).getByText('the parser')).not.toBeVisible();
    expect(within(reasoning).queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText('The parser is fixed.')).toBeVisible();
    expect(screen.getAllByRole('button', { name: 'Copy message' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Fork from here' }));
    expect(onFork).toHaveBeenCalledTimes(1);
  });

  it('keeps streamed reasoning open as its content completes', () => {
    const view = render(
      <EventRow
        event={{ kind: 'thinking', text: 'Inspect the parser', sourceEventId: 1 }}
        blockKey="ajob_1:event:1"
      />,
    );
    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    reasoning.open = true;
    expect(screen.getByText('Inspect the parser')).toBeVisible();

    view.rerender(
      <EventRow
        event={{ kind: 'thinking', text: 'Inspect the parser first.', sourceEventId: 1 }}
        blockKey="ajob_1:event:1"
      />,
    );
    expect(screen.getByLabelText('Agent reasoning')).toBe(reasoning);
    expect(reasoning).toHaveAttribute('open');
    expect(screen.getByText('Inspect the parser first.')).toBeVisible();
  });

  it('separates a recorded summary from detailed reasoning', () => {
    render(
      <EventRow
        event={{
          kind: 'thinking',
          text: 'Inspect the parser.',
          reasoningBlocks: [
            { channel: 'summary', text: 'Inspect the parser.' },
            { channel: 'reasoning', text: 'Compare the token boundaries.' },
          ],
        }}
      />,
    );
    const reasoning = within(screen.getByLabelText('Agent reasoning'));
    expect(reasoning.getByText('Summary')).toBeInTheDocument();
    expect(reasoning.getByText('Detailed reasoning')).toBeInTheDocument();
    expect(reasoning.getAllByText('Inspect the parser.')).toHaveLength(1);
    expect(reasoning.getByText('Compare the token boundaries.')).toBeInTheDocument();
  });

  it('uses a compact status when no reasoning text is available', () => {
    render(<EventRow event={{ kind: 'thinking', text: '   ' }} />);
    expect(screen.getByLabelText('Agent reasoning')).toHaveTextContent('Agent is reasoning');
    expect(screen.queryByText('Reasoning')).not.toBeInTheDocument();
  });
});

describe('per-message actions', () => {
  it.each([
    { role: 'assistant' as const, label: 'Fork from here' },
    { role: 'user' as const, label: 'Edit & rewind' },
  ])('keeps the $label action keyboard reachable and respects busy state', ({ role, label }) => {
    const onAction = vi.fn();
    const message = { ...ROUND, role, content: 'Review this message.' };
    const props = role === 'assistant' ? { onFork: onAction } : { onRewind: onAction };
    const { rerender } = render(<ThreadTurn message={message} {...props} />);

    const action = screen.getByRole('button', { name: label });
    action.focus();
    expect(action).toHaveFocus();
    fireEvent.click(action);
    expect(onAction).toHaveBeenCalledTimes(1);

    rerender(<ThreadTurn message={message} {...props} busy />);
    expect(screen.getByRole('button', { name: label })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: label }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeEnabled();
  });
});

describe('an automatic round', () => {
  it('is a system marker, not a message somebody sent', () => {
    round({ source: 'goal', round: 3 });
    expect(screen.getByText('Round 3 · automatic')).toBeInTheDocument();
    // The generated body is kept for anyone who wants to read it, but it sits
    // behind the marker: nobody typed it, so it gets no user bubble and none of
    // a bubble's actions.
    expect(screen.getByText(ROUND.content).closest('details')).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Copy message' })).not.toBeInTheDocument();
  });

  it('names a repair round rather than calling it work', () => {
    round({ source: 'goal', round: 3, kind: 'repair' });
    expect(screen.getByText('Repair round')).toBeInTheDocument();
  });

  it('shows a completion as the report it is', () => {
    round({ source: 'goal', round: 3, reportParse: 'valid', reportOutcome: 'complete' });
    expect(screen.getByText('complete')).toBeInTheDocument();
  });

  it('shows the reason a valid report carried', () => {
    round({
      source: 'goal',
      round: 3,
      reportParse: 'valid',
      reportOutcome: 'needs_input',
      reportDetail: 'which environment should this deploy to?',
    });
    expect(screen.getByText('needs input')).toBeInTheDocument();
    expect(screen.getByText('which environment should this deploy to?')).toBeInTheDocument();
  });

  it.each(['invalid', 'unavailable'] as const)(
    'says a %s report could not be read, and never that it completed',
    (reportParse) => {
      round({
        source: 'goal',
        round: 4,
        reportParse,
        reportDetail: 'the final message was truncated',
      });
      expect(screen.getByText('the report could not be read')).toBeInTheDocument();
      expect(screen.queryByText('complete')).not.toBeInTheDocument();
    },
  );

  it('says nothing about a round that reliably made no report', () => {
    round({ source: 'goal', round: 2, reportParse: 'absent' });
    expect(screen.queryByText('complete')).not.toBeInTheDocument();
    expect(screen.queryByText('needs input')).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be read/)).not.toBeInTheDocument();
  });
});

describe("a first run's milestones", () => {
  it('say what they mean on the first turn of an owner’s first conversation', () => {
    render(
      <EventRow
        event={{ kind: 'lifecycle', text: 'workspace_preparing' }}
        blockKey="ajob_1:event:2"
        firstRun
      />,
    );

    const label = screen.getByText('Preparing workspace');
    expect(screen.getByText(/Creating an isolated sandbox for this session/)).toBeInTheDocument();
    // The explanation belongs to the row, which keeps its place for navigation.
    expect(label.closest('[data-block-key="ajob_1:event:2"]')).toHaveTextContent(
      /Creating an isolated sandbox/,
    );
  });

  it('are left plain on every other turn', () => {
    render(<EventRow event={{ kind: 'lifecycle', text: 'workspace_preparing' }} />);

    expect(screen.getByText('Preparing workspace')).toBeInTheDocument();
    expect(screen.queryByText(/Creating an isolated sandbox/)).not.toBeInTheDocument();
  });

  it('explain only the steps that need it', () => {
    render(<EventRow event={{ kind: 'lifecycle', text: 'result' }} blockKey="k" firstRun />);

    expect(screen.getByText('Agent finished').closest('[data-block-key="k"]')).toHaveTextContent(
      /^Agent finished$/,
    );
  });
});

describe('a message sent into a running turn', () => {
  it('reads as the owner speaking, where it arrived', () => {
    render(
      <EventRow
        event={{ kind: 'user_message', text: 'also check the docs', sourceEventId: 5 }}
        blockKey="ajob_1:event:5"
      />,
    );

    const bubble = screen.getByText('also check the docs');
    expect(bubble.closest('[data-block-key="ajob_1:event:5"]')).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeInTheDocument();
  });
});

describe('grouped tool calls', () => {
  const jobId = 'ajob_1';
  const renderEvent = (event: AgentEvent, _index: number, blockKey: string) => (
    <EventRow event={event} blockKey={blockKey} />
  );

  it('merges alternating reasoning and tool batches into one inspectable activity', () => {
    const events: AgentEvent[] = [
      { kind: 'thinking', text: 'Inspect the project.', attemptNo: 1, sourceEventId: 1 },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'pwd',
        input: { command: 'pwd' },
        output: ['/workspace'],
        attemptNo: 1,
        sourceEventId: 2,
      },
      {
        kind: 'tool_use',
        tool: 'Read',
        detail: 'README.md',
        input: { file_path: 'README.md' },
        output: ['Project overview'],
        attemptNo: 1,
        sourceEventId: 3,
      },
      { kind: 'thinking', text: 'Find the relevant files.', attemptNo: 1, sourceEventId: 4 },
      {
        kind: 'tool_use',
        tool: 'Glob',
        detail: 'src/**/*.ts',
        input: { pattern: 'src/**/*.ts' },
        output: ['src/router.ts'],
        attemptNo: 1,
        sourceEventId: 5,
      },
      { kind: 'thinking', text: 'Check the implementation.', attemptNo: 1, sourceEventId: 6 },
      {
        kind: 'tool_use',
        tool: 'Read',
        detail: 'src/router.ts',
        input: { file_path: 'src/router.ts', offset: 20 },
        output: ['Router implementation'],
        attemptNo: 1,
        sourceEventId: 7,
      },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'npm test',
        input: { command: 'npm test', timeout: 0 },
        output: ['All tests passed'],
        attemptNo: 1,
        sourceEventId: 8,
      },
      { kind: 'thinking', text: 'Prepare the response.', attemptNo: 1, sourceEventId: 9 },
    ];
    render(<EventList events={events} jobId={jobId} renderEvent={renderEvent} />);

    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    expect(reasoning.tagName).toBe('DETAILS');
    expect(reasoning).not.toHaveAttribute('open');
    const calls = screen.getByRole('list', { name: 'Tool calls' });
    const chips = within(calls).getAllByRole('listitem');
    expect(chips.map((chip) => within(chip).getByText(/^(Bash|Read|Glob):/).textContent)).toEqual([
      'Bash: pwd',
      'Read: README.md',
      'Glob: src/**/*.ts',
      'Read: src/router.ts',
      'Bash: npm test',
    ]);
    for (const chip of chips) {
      expect(within(chip).getByText('Result received')).toBeVisible();
    }
    expect(chips.map((chip) => chip.getAttribute('data-block-key'))).toEqual([
      'ajob_1:e2',
      'ajob_1:e3',
      'ajob_1:e5',
      'ajob_1:e7',
      'ajob_1:e8',
    ]);
    for (const event of events) {
      expect(
        document.querySelectorAll(`[data-block-key="ajob_1:e${event.sourceEventId}"]`),
      ).toHaveLength(1);
      if (event.kind === 'thinking') {
        expect(within(reasoning).getByText(event.text)).not.toBeVisible();
      }
      if (event.kind === 'tool_use')
        expect(screen.queryByText(event.output![0])).not.toBeInTheDocument();
    }
    reasoning.open = true;
    for (const event of events) {
      if (event.kind === 'thinking') expect(within(reasoning).getByText(event.text)).toBeVisible();
    }
    fireEvent.click(screen.getByRole('button', { name: 'Show tool calls' }));
    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    const details = document.getElementById(hide.getAttribute('aria-controls')!)!;
    expect(
      within(details)
        .getAllByText(/· Call \d$/)
        .map((heading) => heading.textContent),
    ).toEqual([
      'Bash · Call 1',
      'Read · Call 2',
      'Glob · Call 3',
      'Read · Call 4',
      'Bash · Call 5',
    ]);
    expect(details).toHaveTextContent('"command": "pwd"');
    expect(details).toHaveTextContent('"file_path": "README.md"');
    expect(details).toHaveTextContent('"pattern": "src/**/*.ts"');
    expect(details).toHaveTextContent('"offset": 20');
    expect(details).toHaveTextContent('"command": "npm test"');
    expect(details).toHaveTextContent('"timeout": 0');
    for (const event of events) {
      if (event.kind === 'tool_use') expect(screen.getByText(event.output![0])).toBeVisible();
    }
    expect(details.querySelectorAll('[data-block-key]')).toHaveLength(0);
  });

  it('folds all merged completed events in Compact and expands their calls in Detailed', () => {
    const events: AgentEvent[] = [
      { kind: 'thinking', text: 'Inspect the files.', attemptNo: 1, sourceEventId: 1 },
      {
        kind: 'tool_use',
        tool: 'Read',
        detail: 'README.md',
        input: { file_path: 'README.md' },
        output: ['Project documentation'],
        resultState: 'received',
        attemptNo: 1,
        sourceEventId: 2,
      },
      { kind: 'thinking', text: 'Verify the change.', attemptNo: 1, sourceEventId: 3 },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'npm test',
        input: { command: 'npm test' },
        output: ['Tests passed'],
        resultState: 'received',
        attemptNo: 1,
        sourceEventId: 4,
      },
      { kind: 'thinking', text: 'Prepare the answer.', attemptNo: 1, sourceEventId: 5 },
      { kind: 'message', text: 'The change is ready.', attemptNo: 1, sourceEventId: 6 },
    ];
    const inspect = vi.fn();
    const props = { events, jobId, renderEvent, completed: true, onInspectEvent: inspect };
    const { rerender } = render(<EventList {...props} presentation="compact" />);
    const answerAnchor = document.querySelector('[data-block-key="ajob_1:e6"]');
    expect(document.querySelector('[data-block-key="ajob_1:e1"]')).toBeNull();
    expect(document.querySelector('[data-block-key="ajob_1:e2"]')).toBeNull();
    expect(answerAnchor).toBeVisible();
    expect(screen.getByText('The change is ready.')).toBeVisible();
    expect(screen.queryByRole('list', { name: 'Tool calls' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show process (5)' }));
    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    expect(reasoning).not.toHaveAttribute('open');
    for (const event of events) {
      if (event.kind === 'thinking') {
        expect(within(reasoning).getByText(event.text)).not.toBeVisible();
      }
    }
    reasoning.open = true;
    const calls = screen.getByRole('list', { name: 'Tool calls' });
    const chips = within(calls).getAllByRole('listitem');
    expect(chips.map((chip) => chip.getAttribute('data-block-key'))).toEqual([
      'ajob_1:e2',
      'ajob_1:e4',
    ]);
    const disclosure = screen.getByRole('button', { name: 'Show tool calls' });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Project documentation')).not.toBeInTheDocument();
    expect(screen.queryByText('Tests passed')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Hide process (5)' }));
    expect(screen.queryByLabelText('Agent reasoning')).not.toBeInTheDocument();

    rerender(<EventList {...props} presentation="detailed" />);
    for (const event of events) {
      expect(
        document.querySelectorAll(`[data-block-key="ajob_1:e${event.sourceEventId}"]`),
      ).toHaveLength(1);
    }
    expect(document.querySelector('[data-block-key="ajob_1:e6"]')).toBe(answerAnchor);
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByText('Project documentation')).toBeVisible();
    expect(screen.getByText('Tests passed')).toBeVisible();
    expect(screen.getByText('The change is ready.')).toBeVisible();
    const restoredReasoning = screen.getByLabelText('Agent reasoning');
    expect(restoredReasoning).toHaveAttribute('open');
    for (const event of events) {
      if (event.kind === 'thinking')
        expect(within(restoredReasoning).getByText(event.text)).toBeVisible();
    }
    expect(
      screen.queryByRole('button', { name: /Show process|Hide process/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect Bash: npm test' }));
    expect(inspect).toHaveBeenCalledWith(events[3], jobId);
  });

  it('keeps all grouped plain text and native reasoning channels in one collapsed section', () => {
    const events: AgentEvent[] = [
      { kind: 'thinking', text: 'Inspect the entry point.', attemptNo: 1, sourceEventId: 1 },
      { kind: 'tool_use', tool: 'Read', detail: 'main.ts', attemptNo: 1, sourceEventId: 2 },
      {
        kind: 'thinking',
        text: 'Combined native text should not be repeated.',
        reasoningBlocks: [
          { channel: 'summary', text: 'Verify the token boundaries.' },
          { channel: 'reasoning', text: 'Compare each token with the parser state.' },
        ],
        attemptNo: 1,
        sourceEventId: 3,
      },
      { kind: 'tool_use', tool: 'Bash', detail: 'npm test', attemptNo: 1, sourceEventId: 4 },
      { kind: 'thinking', text: 'Review the coverage.', attemptNo: 1, sourceEventId: 5 },
      { kind: 'message', text: 'The parser is verified.', attemptNo: 1, sourceEventId: 6 },
    ];
    render(<EventList events={events} jobId={jobId} renderEvent={renderEvent} />);

    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    const anchor = reasoning.closest('[data-block-key]');
    expect(anchor).toHaveAttribute('data-block-key', 'ajob_1:e1');
    expect(anchor).toBeVisible();
    expect(reasoning).not.toHaveAttribute('open');
    const texts = [
      'Inspect the entry point.',
      'Verify the token boundaries.',
      'Compare each token with the parser state.',
      'Review the coverage.',
    ].map((text) => within(reasoning).getByText(text));
    for (const text of texts) expect(text).not.toBeVisible();
    expect(within(reasoning).getByText('Summary')).not.toBeVisible();
    expect(within(reasoning).getByText('Detailed reasoning')).not.toBeVisible();
    expect(
      screen.queryByText('Combined native text should not be repeated.'),
    ).not.toBeInTheDocument();
    expect(screen.getByText('The parser is verified.')).toBeVisible();

    reasoning.open = true;
    for (const text of texts) expect(text).toBeVisible();
    for (let index = 1; index < texts.length; index += 1) {
      expect(
        texts[index - 1].compareDocumentPosition(texts[index]) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
    for (const event of events) {
      expect(
        document.querySelectorAll(`[data-block-key="ajob_1:e${event.sourceEventId}"]`),
      ).toHaveLength(1);
    }
  });

  it('keeps grouped reasoning navigation anchors visible while its disclosure stays collapsed', () => {
    const events: AgentEvent[] = [
      { kind: 'thinking', text: 'Inspect the files.', sourceEventId: 1 },
      { kind: 'thinking', text: 'Verify the change.', sourceEventId: 2 },
      { kind: 'message', text: 'The change is ready.', sourceEventId: 3 },
    ];
    const props = { events, jobId, renderEvent };
    const { rerender } = render(<EventList {...props} presentation="standard" />);
    const reasoning = screen.getByLabelText('Agent reasoning');
    const anchors = events.map((event) =>
      document.querySelector(`[data-block-key="${jobId}:e${event.sourceEventId}"]`),
    );

    for (const presentation of ['compact', 'detailed', 'standard'] as const) {
      rerender(<EventList {...props} presentation={presentation} />);
      expect(screen.getByLabelText('Agent reasoning')).toBe(reasoning);
      expect(reasoning).not.toHaveAttribute('open');
      expect(within(reasoning).getByText('Inspect the files.')).not.toBeVisible();
      expect(within(reasoning).getByText('Verify the change.')).not.toBeVisible();
      expect(document.querySelectorAll('[data-block-key]')).toHaveLength(events.length);
      for (const [index, event] of events.entries()) {
        const anchor = document.querySelector(
          `[data-block-key="${jobId}:e${event.sourceEventId}"]`,
        );
        expect(anchor).toBe(anchors[index]);
        expect(anchor).toBeVisible();
      }
    }
  });

  it('extends a reasoning activity during streaming without replacing its expanded tools', () => {
    const thought: AgentEvent = {
      kind: 'thinking',
      text: 'Start inspecting.',
      attemptNo: 1,
      sourceEventId: 1,
    };
    const first: AgentEvent = {
      kind: 'tool_use',
      tool: 'Read',
      detail: 'README.md',
      input: { file_path: 'README.md' },
      attemptNo: 1,
      sourceEventId: 2,
    };
    const middle: AgentEvent = {
      kind: 'thinking',
      text: 'Run the tests next.',
      attemptNo: 1,
      sourceEventId: 3,
    };
    const next: AgentEvent = {
      kind: 'tool_use',
      tool: 'Bash',
      detail: 'npm test',
      input: { command: 'npm test' },
      attemptNo: 1,
      sourceEventId: 4,
    };
    const { rerender } = render(
      <EventList events={[thought]} jobId={jobId} renderEvent={renderEvent} />,
    );
    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    expect(screen.getByText('Start inspecting.')).not.toBeVisible();
    reasoning.open = true;
    expect(screen.getByText('Start inspecting.')).toBeVisible();
    expect(screen.queryByRole('list', { name: 'Tool calls' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show tool calls?/ })).not.toBeInTheDocument();

    rerender(
      <EventList events={[thought, first, middle]} jobId={jobId} renderEvent={renderEvent} />,
    );
    expect(screen.getByLabelText('Agent reasoning')).toBe(reasoning);
    expect(reasoning).toHaveAttribute('open');
    expect(screen.getByText('Start inspecting.')).toBeVisible();
    expect(screen.getByText('Run the tests next.')).toBeVisible();
    const calls = screen.getByRole('list', { name: 'Tool calls' });
    const firstChip = screen.getByRole('listitem');
    const show = screen.getByRole('button', { name: 'Show tool call' });
    const detailsId = show.getAttribute('aria-controls');
    fireEvent.click(show);

    const growing: AgentEvent[] = [
      thought,
      { ...first, output: ['Project documentation'] },
      middle,
      next,
      { kind: 'thinking', text: 'Review the results.', attemptNo: 1, sourceEventId: 5 },
    ];
    rerender(<EventList events={growing} jobId={jobId} renderEvent={renderEvent} />);
    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    expect(hide).toBe(show);
    expect(hide).toHaveAttribute('aria-expanded', 'true');
    expect(hide).toHaveAttribute('aria-controls', detailsId);
    expect(screen.getByRole('list', { name: 'Tool calls' })).toBe(calls);
    expect(screen.getAllByRole('listitem')[0]).toBe(firstChip);
    expect(screen.getByLabelText('Agent reasoning')).toBe(reasoning);
    expect(reasoning).toHaveAttribute('open');
    expect(screen.getByText('Review the results.')).toBeVisible();
    expect(screen.getByText('Project documentation')).toBeVisible();
    expect(screen.getByText('Waiting for the tool result…')).toBeVisible();

    rerender(
      <EventList
        events={growing.map((event) =>
          event.sourceEventId === 4 ? { ...next, output: ['Tests passed'] } : event,
        )}
        jobId={jobId}
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toBe(hide);
    expect(screen.getByText('Tests passed')).toBeVisible();
    expect(screen.queryByText('Waiting for the tool result…')).not.toBeInTheDocument();
    expect(screen.getAllByRole('listitem')[0]).toBe(firstChip);
    expect(screen.getByLabelText('Agent reasoning')).toBe(reasoning);
    expect(reasoning).toHaveAttribute('open');
    for (const event of growing) {
      if (event.kind === 'thinking') expect(within(reasoning).getByText(event.text)).toBeVisible();
    }
    expect(document.querySelectorAll('[data-block-key]')).toHaveLength(5);
  });

  it('keeps reasoning activities separate at delegation, attempt and message boundaries', () => {
    const events: AgentEvent[] = [
      { kind: 'thinking', text: 'Inspect the main task.', attemptNo: 1, sourceEventId: 1 },
      { kind: 'tool_use', tool: 'Read', detail: 'main.ts', attemptNo: 1, sourceEventId: 2 },
      { kind: 'thinking', text: 'Wait for the child.', attemptNo: 1, sourceEventId: 3 },
      {
        kind: 'tool_use',
        tool: 'Read',
        detail: 'child.ts',
        attemptNo: 1,
        sourceEventId: 4,
        parentToolUseId: 'child',
      },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'child tests',
        attemptNo: 1,
        sourceEventId: 5,
        parentToolUseId: 'child',
      },
      { kind: 'thinking', text: 'Continue the main task.', attemptNo: 1, sourceEventId: 6 },
      { kind: 'tool_use', tool: 'Bash', detail: 'main tests', attemptNo: 1, sourceEventId: 7 },
      { kind: 'tool_use', tool: 'Read', detail: 'retry.ts', attemptNo: 2, sourceEventId: 8 },
      { kind: 'thinking', text: 'Review the retry.', attemptNo: 2, sourceEventId: 9 },
      { kind: 'message', text: 'The retry is ready.', attemptNo: 2, sourceEventId: 10 },
      { kind: 'thinking', text: 'Inspect the docs.', attemptNo: 2, sourceEventId: 11 },
      { kind: 'tool_use', tool: 'Read', detail: 'docs.md', attemptNo: 2, sourceEventId: 12 },
      { kind: 'user_message', text: 'Check the examples too.', attemptNo: 2, sourceEventId: 13 },
      { kind: 'thinking', text: 'Inspect the examples.', attemptNo: 2, sourceEventId: 14 },
      { kind: 'tool_use', tool: 'Read', detail: 'examples.ts', attemptNo: 2, sourceEventId: 15 },
    ];
    const renderStructured = vi.fn(renderEvent);
    render(<EventList events={events} jobId={jobId} renderEvent={renderStructured} />);

    const groups = screen.getAllByRole('list', { name: 'Tool calls' });
    expect(groups.map((group) => within(group).getAllByRole('listitem').length)).toEqual([
      1, 2, 1, 1, 1, 1,
    ]);
    const reasoningSections = screen.getAllByLabelText('Agent reasoning');
    expect(reasoningSections).toHaveLength(5);
    for (const section of reasoningSections) {
      expect(section.tagName).toBe('DETAILS');
      expect(section).not.toHaveAttribute('open');
    }
    for (const event of events) {
      if (event.kind === 'thinking') expect(screen.getByText(event.text)).not.toBeVisible();
    }
    expect(within(groups[1]).getAllByText('Subagent tool')).toHaveLength(2);
    for (const group of [groups[0], ...groups.slice(2)]) {
      expect(within(group).queryByText('Subagent tool')).not.toBeInTheDocument();
    }
    expect(renderStructured).toHaveBeenCalledWith(events[9], 9, 'ajob_1:e10');
    expect(renderStructured).toHaveBeenCalledWith(events[12], 12, 'ajob_1:e13');
    const assistantMessage = screen.getByText('The retry is ready.');
    const userMessage = screen.getByText('Check the examples too.');
    expect(
      groups[3].compareDocumentPosition(assistantMessage) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      assistantMessage.compareDocumentPosition(groups[4]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      groups[4].compareDocumentPosition(userMessage) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      userMessage.compareDocumentPosition(groups[5]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(document.querySelectorAll('[data-block-key]')).toHaveLength(events.length);
  });

  it('keeps an expanded tool-only activity stable when reasoning is appended', () => {
    const first: AgentEvent = {
      kind: 'tool_use',
      tool: 'Read',
      detail: 'README.md',
      input: { file_path: 'README.md' },
      output: ['Project documentation'],
      attemptNo: 1,
      sourceEventId: 1,
    };
    const { rerender } = render(
      <EventList events={[first]} jobId={jobId} renderEvent={renderEvent} />,
    );
    const chip = screen.getByRole('listitem');
    const show = screen.getByRole('button', { name: 'Show tool call' });
    const detailsId = show.getAttribute('aria-controls');
    fireEvent.click(show);

    rerender(
      <EventList
        events={[
          first,
          { kind: 'thinking', text: 'Run verification.', attemptNo: 1, sourceEventId: 2 },
          {
            kind: 'tool_use',
            tool: 'Bash',
            detail: 'npm test',
            input: { command: 'npm test' },
            output: ['Tests passed'],
            attemptNo: 1,
            sourceEventId: 3,
          },
        ]}
        jobId={jobId}
        renderEvent={renderEvent}
      />,
    );

    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    expect(hide).toBe(show);
    expect(hide).toHaveAttribute('aria-expanded', 'true');
    expect(hide).toHaveAttribute('aria-controls', detailsId);
    expect(screen.getAllByRole('listitem')[0]).toBe(chip);
    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    expect(reasoning).not.toHaveAttribute('open');
    expect(within(reasoning).getByText('Run verification.')).not.toBeVisible();
    reasoning.open = true;
    expect(within(reasoning).getByText('Run verification.')).toBeVisible();
    expect(screen.getByText('Project documentation')).toBeVisible();
    expect(screen.getByText('Tests passed')).toBeVisible();
    expect(document.querySelectorAll('[data-block-key]')).toHaveLength(3);
  });

  it.each([true, false])(
    'respects activity card boundaries while grouping reasoning when details are %s',
    (showActivityDetails) => {
      render(
        <EventList
          jobId={jobId}
          renderEvent={renderEvent}
          showActivityDetails={showActivityDetails}
          events={[
            { kind: 'thinking', text: 'Inspect first.', attemptNo: 1, sourceEventId: 1 },
            { kind: 'tool_use', tool: 'Read', detail: 'README.md', attemptNo: 1, sourceEventId: 2 },
            { kind: 'thinking', text: 'Delegate the review.', attemptNo: 1, sourceEventId: 3 },
            {
              kind: 'tool_use',
              tool: 'Task',
              detail: 'Review tests',
              input: { description: 'Review tests', prompt: 'Check test coverage.' },
              attemptNo: 1,
              sourceEventId: 4,
            },
            { kind: 'thinking', text: 'Run the tests.', attemptNo: 1, sourceEventId: 5 },
            { kind: 'tool_use', tool: 'Bash', detail: 'npm test', attemptNo: 1, sourceEventId: 6 },
          ]}
        />,
      );

      const groups = screen.getAllByRole('list', { name: 'Tool calls' });
      expect(groups.map((group) => within(group).getAllByRole('listitem').length)).toEqual(
        showActivityDetails ? [1, 1] : [3],
      );
      const reasoningSections = screen.getAllByLabelText('Agent reasoning');
      expect(reasoningSections).toHaveLength(showActivityDetails ? 2 : 1);
      for (const section of reasoningSections) {
        expect(section.tagName).toBe('DETAILS');
        expect(section).not.toHaveAttribute('open');
      }
      for (const text of ['Inspect first.', 'Delegate the review.', 'Run the tests.']) {
        expect(screen.getByText(text)).not.toBeVisible();
      }
      expect(document.querySelectorAll('[data-block-key]')).toHaveLength(6);
      if (showActivityDetails) {
        const card = screen.getByRole('region', { name: 'Subagent activity' });
        expect(card).toHaveAttribute('data-block-key', 'ajob_1:e4');
        expect(
          groups[0].compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING,
        ).toBeTruthy();
        expect(
          card.compareDocumentPosition(groups[1]) & Node.DOCUMENT_POSITION_FOLLOWING,
        ).toBeTruthy();
      } else {
        expect(screen.queryByRole('region', { name: 'Subagent activity' })).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Show tool calls' }));
        expect(screen.getByText(/"prompt": "Check test coverage\."/)).toBeVisible();
      }
    },
  );

  it('keeps activity calls compact with inspectable arguments and results when their overview is elsewhere', () => {
    const events: AgentEvent[] = [
      {
        kind: 'tool_use',
        tool: 'Task',
        detail: 'Review',
        sourceEventId: 1,
        activityKey: '1:delegate',
        input: { description: 'Review', prompt: 'Inspect coverage.' },
        output: ['Started in background.'],
      },
      {
        kind: 'tool_use',
        tool: 'update_plan',
        detail: 'Plan',
        sourceEventId: 2,
        input: { plan: [{ step: 'Review tests', status: 'pending' }] },
        output: ['Plan recorded.'],
      },
    ];
    const { rerender } = render(
      <EventList
        events={events}
        jobId={jobId}
        renderEvent={renderEvent}
        showActivityDetails={false}
      />,
    );

    const calls = screen.getByRole('list', { name: 'Tool calls' });
    const chips = within(calls).getAllByRole('listitem');
    expect(chips).toHaveLength(2);
    expect(chips.map((chip) => chip.getAttribute('data-block-key'))).toEqual([
      'ajob_1:activity:1:delegate',
      'ajob_1:e2',
    ]);
    expect(screen.queryByRole('region', { name: 'Task activity' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Subagent activity' })).not.toBeInTheDocument();
    expect(screen.queryByText('Started in background.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show tool calls' }));
    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    const details = document.getElementById(hide.getAttribute('aria-controls')!)!;
    expect(details).toHaveTextContent('"prompt": "Inspect coverage."');
    expect(details).toHaveTextContent('"status": "pending"');
    expect(screen.getByText('Started in background.')).toBeVisible();
    expect(screen.getByText('Plan recorded.')).toBeVisible();
    expect(details.querySelectorAll('[data-block-key]')).toHaveLength(0);

    rerender(<EventList events={events} jobId={jobId} renderEvent={renderEvent} />);
    expect(screen.getByRole('region', { name: 'Task activity' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Subagent activity' })).toBeVisible();
  });

  it('forwards compact activity rendering through historical turns', () => {
    render(
      <HistoryTurn
        busy={false}
        showActivityDetails={false}
        turn={{
          key: 'history',
          jobId: 'history',
          displayNo: 1,
          messages: [],
          fallbackResponses: [],
          promptCard: null,
          forkable: false,
          toolCalls: 1,
          prompt: '',
          lastResponse: '',
          isCurrent: false,
          events: [
            {
              kind: 'tool_use',
              tool: 'Task',
              detail: 'Review',
              input: { description: 'Review' },
              sourceEventId: 1,
            },
          ],
        }}
      />,
    );
    expect(screen.getByRole('list', { name: 'Tool calls' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Show tool call' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Subagent activity' })).not.toBeInTheDocument();
  });

  it('keeps task and delegation cards visible between ordinary tool groups', () => {
    const events: AgentEvent[] = [
      { kind: 'tool_use', tool: 'Read', detail: 'README.md', sourceEventId: 1 },
      { kind: 'tool_use', tool: 'Bash', detail: 'npm test', sourceEventId: 2 },
      {
        kind: 'tool_use',
        tool: 'Task',
        detail: 'Review tests',
        sourceEventId: 3,
        input: { description: 'Review tests', prompt: 'Check the tests.' },
        resultState: 'waiting',
      },
      {
        kind: 'tool_use',
        tool: 'Read',
        detail: 'tests.ts',
        sourceEventId: 4,
        parentToolUseId: 'delegate',
      },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'npm test',
        sourceEventId: 5,
        parentToolUseId: 'delegate',
      },
      {
        kind: 'tool_use',
        tool: 'update_plan',
        detail: 'Plan',
        sourceEventId: 6,
        input: {
          plan: [
            { step: 'Inspect', status: 'completed' },
            { step: 'Review', status: 'pending' },
          ],
        },
      },
      { kind: 'tool_use', tool: 'Read', detail: 'src/app.ts', sourceEventId: 7 },
    ];
    const renderStructured = vi.fn(renderEvent);
    render(<EventList events={events} jobId={jobId} renderEvent={renderStructured} />);

    const groups = screen.getAllByRole('list', { name: 'Tool calls' });
    expect(groups.map((group) => within(group).getAllByRole('listitem').length)).toEqual([2, 2, 1]);
    const delegation = screen.getByRole('region', { name: 'Subagent activity' });
    const plan = screen.getByRole('region', { name: 'Task activity' });
    expect(delegation).toBeVisible();
    expect(delegation).toHaveAttribute('data-block-key', 'ajob_1:e3');
    expect(within(delegation).getByText('Review tests')).toBeVisible();
    expect(within(plan).getByText('1 of 2 marked completed')).toBeVisible();
    expect(renderStructured).toHaveBeenNthCalledWith(1, events[2], 2, 'ajob_1:e3');
    expect(renderStructured).toHaveBeenNthCalledWith(2, events[5], 5, 'ajob_1:e6');
    expect(
      groups[0].compareDocumentPosition(delegation) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      delegation.compareDocumentPosition(groups[1]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(groups[1].compareDocumentPosition(plan) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('keeps activity calls compact and their raw details available when an overview owns them', () => {
    const events: AgentEvent[] = [
      {
        kind: 'tool_use',
        tool: 'Task',
        detail: 'Review tests',
        sourceEventId: 3,
        input: { description: 'Review tests', prompt: 'Check the tests.' },
        output: ['Started in background.'],
        resultState: 'received',
      },
      {
        kind: 'tool_use',
        tool: 'update_plan',
        detail: 'Plan',
        sourceEventId: 6,
        input: { plan: [{ step: 'Review', status: 'in_progress' }] },
        output: ['Plan updated.'],
        resultState: 'received',
      },
    ];

    render(
      <EventList
        events={events}
        jobId={jobId}
        renderEvent={renderEvent}
        showActivityDetails={false}
      />,
    );

    expect(screen.queryByRole('region', { name: 'Subagent activity' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Task activity' })).not.toBeInTheDocument();
    const chips = within(screen.getByRole('list', { name: 'Tool calls' })).getAllByRole('listitem');
    expect(chips.map((chip) => chip.getAttribute('data-block-key'))).toEqual([
      'ajob_1:e3',
      'ajob_1:e6',
    ]);

    fireEvent.click(screen.getByRole('button', { name: 'Show tool calls' }));
    expect(screen.getByText('Started in background.')).toBeVisible();
    expect(screen.getByText('Plan updated.')).toBeVisible();
    expect(screen.getByText(/"description": "Review tests"/)).toBeVisible();
    expect(screen.getByText(/"step": "Review"/)).toBeVisible();
  });

  it('groups child calls only with calls from the same recorded delegation', () => {
    render(
      <EventList
        jobId={jobId}
        renderEvent={renderEvent}
        events={[
          { kind: 'tool_use', tool: 'Bash', detail: 'main', sourceEventId: 1 },
          {
            kind: 'tool_use',
            tool: 'Read',
            detail: 'child a',
            sourceEventId: 2,
            parentToolUseId: 'a',
          },
          {
            kind: 'tool_use',
            tool: 'Bash',
            detail: 'child a tests',
            sourceEventId: 3,
            parentToolUseId: 'a',
          },
          {
            kind: 'tool_use',
            tool: 'Read',
            detail: 'child b',
            sourceEventId: 4,
            parentToolUseId: 'b',
          },
        ]}
      />,
    );

    const groups = screen.getAllByRole('list', { name: 'Tool calls' });
    expect(groups.map((group) => within(group).getAllByRole('listitem').length)).toEqual([1, 2, 1]);
    expect(within(groups[0]).queryByText('Subagent tool')).not.toBeInTheDocument();
    expect(within(groups[1]).getAllByText('Subagent tool')).toHaveLength(2);
    expect(within(groups[2]).getByText('Subagent tool')).toHaveAttribute('title', 'Delegated by b');
    expect(document.querySelectorAll('[data-block-key]')).toHaveLength(4);
  });

  it('keeps ended, ambiguous and interrupted generic calls from appearing to wait', () => {
    render(
      <EventList
        jobId={jobId}
        renderEvent={renderEvent}
        events={[
          {
            kind: 'tool_use',
            tool: 'Bash',
            detail: 'ended',
            sourceEventId: 1,
            resultState: 'not_recorded',
          },
          {
            kind: 'tool_use',
            tool: 'Read',
            detail: 'ambiguous',
            sourceEventId: 2,
            resultState: 'unknown',
          },
          {
            kind: 'tool_use',
            tool: 'Write',
            detail: 'interrupted',
            sourceEventId: 3,
            resultState: 'cancelled',
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Show tool calls' }));
    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    const details = document.getElementById(hide.getAttribute('aria-controls')!)!;
    expect(within(details).getByText('Result not recorded')).toBeVisible();
    expect(within(details).getByText('Pairing unknown')).toBeVisible();
    expect(within(details).getByText('Call interrupted')).toBeVisible();
    expect(screen.queryByText('Waiting for the tool result…')).not.toBeInTheDocument();
  });

  it('retains a native delegation card and its anchor when an earlier start backfills', () => {
    const call: AgentEvent = {
      kind: 'tool_use',
      tool: 'spawn_agent',
      detail: 'Review',
      activityKey: '1:delegate',
      sourceEventId: 20,
      input: { description: 'Review' },
    };
    const { rerender } = render(
      <EventList events={[call]} jobId={jobId} renderEvent={renderEvent} />,
    );
    const card = screen.getByRole('region', { name: 'Subagent activity' });
    expect(card).toHaveAttribute('data-block-key', 'ajob_1:activity:1:delegate');
    rerender(
      <EventList
        events={[{ ...call, sourceEventId: 10 }]}
        jobId={jobId}
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByRole('region', { name: 'Subagent activity' })).toBe(card);
    expect(document.querySelectorAll('[data-block-key]')).toHaveLength(1);
  });

  it('shows adjacent tools together with one disclosure for their complete calls and results', () => {
    render(
      <EventList
        jobId={jobId}
        renderEvent={renderEvent}
        events={[
          {
            kind: 'tool_use',
            tool: 'Bash',
            detail: 'pytest -q',
            input: { command: 'pytest -q', cwd: '/workspace', timeout: 0 },
            output: ['2 passed'],
            attemptNo: 1,
            sourceEventId: 10,
          },
          {
            kind: 'tool_use',
            tool: 'Read',
            detail: 'src/router.ts',
            input: { file_path: 'src/router.ts', offset: 20 },
            output: ['router source'],
            attemptNo: 1,
            sourceEventId: 11,
          },
        ]}
      />,
    );

    const calls = screen.getByRole('list', { name: 'Tool calls' });
    const chips = within(calls).getAllByRole('listitem');
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent('Bash');
    expect(chips[1]).toHaveTextContent('Read');
    expect(chips.map((chip) => chip.getAttribute('data-block-key'))).toEqual([
      'ajob_1:e10',
      'ajob_1:e11',
    ]);
    const show = screen.getByRole('button', { name: 'Show tool calls' });
    expect(screen.getAllByRole('button', { name: /Show tool calls?/ })).toHaveLength(1);
    expect(show).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('2 passed')).not.toBeInTheDocument();
    expect(screen.queryByText('router source')).not.toBeInTheDocument();

    fireEvent.click(show);
    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    expect(hide).toHaveAttribute('aria-expanded', 'true');
    const details = document.getElementById(hide.getAttribute('aria-controls')!);
    expect(details).toBeVisible();
    expect(details).toHaveTextContent('"cwd": "/workspace"');
    expect(details).toHaveTextContent('"timeout": 0');
    expect(details).toHaveTextContent('"file_path": "src/router.ts"');
    expect(details).toHaveTextContent('"offset": 20');
    expect(screen.getByText('2 passed')).toBeVisible();
    expect(screen.getByText('router source')).toBeVisible();
    expect(details?.querySelectorAll('[data-block-key]')).toHaveLength(0);

    fireEvent.click(hide);
    expect(screen.queryByText('2 passed')).not.toBeInTheDocument();
    expect(screen.queryByText('router source')).not.toBeInTheDocument();
  });

  it('keeps messages and attempts between groups and passes original event indexes to the renderer', () => {
    const events: AgentEvent[] = [
      { kind: 'tool_use', tool: 'Read', detail: 'README.md', attemptNo: 1, sourceEventId: 10 },
      { kind: 'message', text: 'Checking the implementation.', attemptNo: 1, sourceEventId: 11 },
      { kind: 'tool_use', tool: 'Bash', detail: 'pytest', attemptNo: 1, sourceEventId: 12 },
      { kind: 'tool_use', tool: 'Read', detail: 'src/app.ts', attemptNo: 2, sourceEventId: 13 },
      { kind: 'tool_use', tool: 'Bash', detail: 'npm test', attemptNo: 2, sourceEventId: 14 },
      { kind: 'user_message', text: 'Also check the docs.', attemptNo: 2, sourceEventId: 15 },
      { kind: 'tool_use', tool: 'Read', detail: 'docs/api.md', attemptNo: 2, sourceEventId: 16 },
    ];
    const renderNonTool = vi.fn(renderEvent);
    render(<EventList events={events} jobId={jobId} renderEvent={renderNonTool} />);

    const groups = screen.getAllByRole('list', { name: 'Tool calls' });
    expect(groups.map((group) => within(group).getAllByRole('listitem').length)).toEqual([
      1, 1, 2, 1,
    ]);
    expect(screen.getAllByRole('button', { name: 'Show tool call' })).toHaveLength(3);
    expect(screen.getAllByRole('button', { name: 'Show tool calls' })).toHaveLength(1);
    expect(renderNonTool).toHaveBeenCalledTimes(2);
    expect(renderNonTool).toHaveBeenNthCalledWith(1, events[1], 1, 'ajob_1:e11');
    expect(renderNonTool).toHaveBeenNthCalledWith(2, events[5], 5, 'ajob_1:e15');

    const implementation = screen.getByText('Checking the implementation.');
    const docs = screen.getByText('Also check the docs.');
    expect(
      groups[0].compareDocumentPosition(implementation) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      implementation.compareDocumentPosition(groups[1]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(groups[2].compareDocumentPosition(docs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(docs.compareDocumentPosition(groups[3]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('keeps the disclosure open and existing anchors when another call and results arrive', () => {
    const first: AgentEvent = {
      kind: 'tool_use',
      tool: 'Bash',
      detail: 'pytest -q',
      input: { command: 'pytest -q' },
      attemptNo: 1,
      sourceEventId: 21,
    };
    const next: AgentEvent = {
      kind: 'tool_use',
      tool: 'Read',
      detail: 'README.md',
      input: { file_path: 'README.md' },
      attemptNo: 1,
      sourceEventId: 22,
    };
    const { rerender } = render(
      <EventList events={[first]} jobId={jobId} renderEvent={renderEvent} />,
    );
    const firstChip = screen.getByRole('listitem');
    const show = screen.getByRole('button', { name: 'Show tool call' });
    const detailsId = show.getAttribute('aria-controls');
    fireEvent.click(show);
    expect(screen.getByText('Waiting for the tool result…')).toBeVisible();

    rerender(
      <EventList
        events={[{ ...first, output: ['2 passed'] }, next]}
        jobId={jobId}
        renderEvent={renderEvent}
      />,
    );
    const hide = screen.getByRole('button', { name: 'Hide tool calls' });
    expect(hide).toHaveAttribute('aria-expanded', 'true');
    expect(hide).toHaveAttribute('aria-controls', detailsId);
    const chips = screen.getAllByRole('listitem');
    expect(chips[0]).toBe(firstChip);
    expect(chips.map((chip) => chip.getAttribute('data-block-key'))).toEqual([
      'ajob_1:e21',
      'ajob_1:e22',
    ]);
    expect(screen.getByText('2 passed')).toBeVisible();
    expect(screen.getByText('Waiting for the tool result…')).toBeVisible();

    rerender(
      <EventList
        events={[
          { ...first, output: ['2 passed'] },
          { ...next, output: ['project documentation'] },
        ]}
        jobId={jobId}
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByText('project documentation')).toBeVisible();
    expect(screen.queryByText('Waiting for the tool result…')).not.toBeInTheDocument();
    expect(screen.getAllByRole('listitem')[0]).toBe(firstChip);
  });
});

describe('transcript work details', () => {
  const call: AgentEvent = {
    kind: 'tool_use',
    tool: 'Bash',
    detail: 'Run checks',
    input: { command: 'npm test' },
    resultState: 'received',
    output: ['Checks passed'],
    sourceEventId: 1,
  };
  const answer: AgentEvent = { kind: 'message', text: 'The change is ready.', sourceEventId: 2 };
  const renderEvent = (event: AgentEvent, _index: number, blockKey: string) => (
    <EventRow event={event} blockKey={blockKey} />
  );

  it('folds recorded completed process without hiding the answer or changing its anchors', () => {
    const { rerender } = render(
      <EventList
        events={[call, answer]}
        jobId="done"
        completed
        presentation="compact"
        renderEvent={renderEvent}
      />,
    );
    const answerAnchor = document.querySelector('[data-block-key="done:e2"]');
    expect(document.querySelector('[data-block-key="done:e1"]')).toBeNull();
    expect(screen.getByText('The change is ready.')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Show process (1)' }));
    const toolAnchor = document.querySelector('[data-block-key="done:e1"]');
    expect(toolAnchor).toBeVisible();
    rerender(
      <EventList
        events={[call, answer]}
        jobId="done"
        completed
        presentation="detailed"
        renderEvent={renderEvent}
      />,
    );
    expect(document.querySelector('[data-block-key="done:e1"]')).toBe(toolAnchor);
    expect(document.querySelector('[data-block-key="done:e2"]')).toBe(answerAnchor);
    expect(screen.getByText('Checks passed')).toBeVisible();
    expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
  });

  it('keeps interrupted, failed, unresolved, and diagnostic states visible in compact completed turns', () => {
    const events: AgentEvent[] = [
      { ...call, resultState: 'failed', outputIsError: true },
      { ...call, resultState: 'cancelled', sourceEventId: 3 },
      { ...call, resultState: 'unknown', sourceEventId: 4 },
      { ...call, resultState: 'not_recorded', sourceEventId: 5 },
      {
        kind: 'tool_result',
        text: 'An unmatched recorded result',
        isError: false,
        sourceEventId: 6,
      },
      { kind: 'lifecycle', text: 'unexpected_runtime_phase', sourceEventId: 7 },
      { kind: 'user_message', text: 'Please also check accessibility.', sourceEventId: 8 },
      { kind: 'lifecycle', text: 'cancelled_by_owner', sourceEventId: 9 },
      { kind: 'lifecycle', text: 'attempt_superseded', sourceEventId: 10 },
      answer,
    ];
    render(
      <EventList
        events={events}
        jobId="partial"
        completed
        presentation="compact"
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByText('Call failed')).toBeVisible();
    expect(screen.getByText('Call interrupted')).toBeVisible();
    expect(screen.getByText('Pairing unknown')).toBeVisible();
    expect(screen.getByText('Result not recorded')).toBeVisible();
    expect(screen.getByText('Unmatched tool result')).toBeVisible();
    expect(document.querySelector('[data-block-key="partial:e7"]')).toBeVisible();
    expect(screen.getByText('Please also check accessibility.')).toBeVisible();
    expect(screen.getByText('Stopped by you')).toBeVisible();
    expect(screen.getByText('Run interrupted')).toBeVisible();
    expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
    expect(document.querySelectorAll('[data-block-key]')).toHaveLength(events.length);
  });

  it('does not fold a running turn merely because one tool returned', () => {
    render(
      <EventList
        events={[call, answer]}
        jobId="running"
        presentation="compact"
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByText('Bash')).toBeVisible();
    expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
  });

  it('keeps semantic chips visible in Standard while Compact folds completed process', () => {
    const { rerender } = render(
      <EventList
        events={[call, answer]}
        jobId="done"
        completed
        presentation="standard"
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByText('Bash: npm test')).toBeVisible();
    expect(screen.queryByText('Checks passed')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
    rerender(
      <EventList
        events={[call, answer]}
        jobId="done"
        completed
        presentation="compact"
        renderEvent={renderEvent}
      />,
    );
    expect(screen.queryByText('Bash')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show process (1)' })).toBeVisible();
    expect(screen.getByText('The change is ready.')).toBeVisible();
  });

  it.each<AgentEvent>([
    { ...call, resultState: 'failed', outputIsError: true, sourceEventId: 3 },
    { ...call, resultState: 'unknown', sourceEventId: 3 },
    { ...call, resultState: 'waiting', output: undefined, sourceEventId: 3 },
    {
      kind: 'tool_use',
      tool: 'update_plan',
      detail: 'Review changes',
      input: { plan: [{ step: 'Review changes', status: 'pending' }] },
      resultState: 'received',
      output: ['Plan recorded.'],
      sourceEventId: 3,
    },
    { kind: 'terminal', text: 'Unclassified terminal output', sourceEventId: 3 },
  ])('folds successful activity around an attention record ($kind, $resultState)', (attention) => {
    const events: AgentEvent[] = [
      { kind: 'thinking', text: 'Inspect the change.', sourceEventId: 10 },
      call,
      attention,
      {
        ...call,
        tool: 'Read',
        input: { file_path: 'README.md' },
        output: ['Project documentation'],
        sourceEventId: 4,
      },
      answer,
    ];
    const props = {
      events,
      jobId: 'mixed',
      completed: true,
      showActivityDetails: false,
      renderEvent,
    };
    const { rerender } = render(<EventList {...props} presentation="standard" />);
    const anchors = events.map((event) =>
      document.querySelector(`[data-block-key="mixed:e${event.sourceEventId}"]`),
    );
    const currentAnchor = (index: number) =>
      document.querySelector(`[data-block-key="mixed:e${events[index].sourceEventId}"]`);
    const reasoning = anchors[0];
    const reasoningSummary = reasoning?.querySelector(':scope > summary') ?? reasoning;
    expect(reasoningSummary).toBeVisible();
    for (const anchor of anchors.slice(1)) expect(anchor).toBeVisible();
    if (reasoning?.tagName === 'DETAILS') {
      expect(reasoning).not.toHaveAttribute('open');
      expect(screen.getByText('Inspect the change.')).not.toBeVisible();
    }

    rerender(<EventList {...props} presentation="compact" />);
    expect(currentAnchor(0)).toBeNull();
    expect(currentAnchor(1)).toBeNull();
    expect(anchors[2]).toBeVisible();
    expect(currentAnchor(3)).toBeNull();
    expect(anchors[4]).toBeVisible();
    expect(screen.getByRole('button', { name: 'Show process (2)' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Show process (1)' }));
    const restoredToolAnchor = currentAnchor(3);
    expect(restoredToolAnchor).toBeVisible();
    expect(currentAnchor(2)).toBe(anchors[2]);
    expect(currentAnchor(4)).toBe(anchors[4]);

    rerender(<EventList {...props} presentation="detailed" />);
    expect(screen.getByLabelText('Agent reasoning').querySelector('summary')).toBeVisible();
    expect(currentAnchor(3)).toBe(restoredToolAnchor);
    expect(screen.getByText('Project documentation')).toBeVisible();
    for (const output of screen.getAllByText('Checks passed')) expect(output).toBeVisible();
    expect(
      screen.queryByRole('button', { name: /Show process|Hide process/ }),
    ).not.toBeInTheDocument();
    rerender(<EventList {...props} presentation="compact" />);
    expect(currentAnchor(0)).toBeNull();
    expect(screen.getByRole('button', { name: 'Show process (1)' })).toBeVisible();
    expect(currentAnchor(3)).toBeNull();
    expect(screen.getByText('The change is ready.')).toBeVisible();
  });

  it('keeps mixed successful and pending activity visible while the turn is running', () => {
    render(
      <EventList
        events={[
          call,
          { ...call, resultState: 'waiting', output: undefined, sourceEventId: 3 },
          { ...call, tool: 'Read', input: { file_path: 'README.md' }, sourceEventId: 4 },
        ]}
        jobId="running"
        presentation="compact"
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByRole('list', { name: 'Tool calls' })).toBeVisible();
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(screen.getByText('Waiting for result')).toBeVisible();
    expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
  });

  it('opens the existing inspector from a descriptive summary with its original event and job', () => {
    const inspect = vi.fn();
    render(
      <EventList
        events={[call]}
        jobId="historical"
        presentation="standard"
        onInspectEvent={inspect}
        renderEvent={renderEvent}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Inspect Bash: npm test' }));
    expect(inspect).toHaveBeenCalledWith(call, 'historical');
    expect(screen.getByText('Result received')).toBeVisible();
  });

  it('expands task cards and their recorded details with detailed mode even when overview cards are elsewhere', () => {
    const task: AgentEvent = {
      kind: 'tool_use',
      tool: 'update_plan',
      detail: 'Review',
      sourceEventId: 3,
      input: { plan: [{ step: 'Inspect coverage', status: 'completed' }] },
      output: ['Plan recorded.'],
      resultState: 'received',
    };
    render(
      <EventList
        events={[task]}
        jobId="task"
        presentation="detailed"
        showActivityDetails={false}
        renderEvent={renderEvent}
      />,
    );
    expect(screen.getByRole('region', { name: 'Task activity' })).toBeVisible();
    expect(screen.getByText('Inspect coverage')).toBeVisible();
    expect(screen.getByText('Plan recorded.')).toBeVisible();
  });

  it('keeps a recorded subagent failure visible even when its tool returned normally', () => {
    const task: AgentEvent = {
      kind: 'tool_use',
      tool: 'wait',
      detail: 'Wait for reviewer',
      sourceEventId: 3,
      input: { ids: ['reviewer'] },
      output: [JSON.stringify({ status: { reviewer: { errored: 'Could not review' } } })],
      resultState: 'received',
    };
    render(
      <EventList
        events={[task, answer]}
        jobId="task"
        completed
        presentation="compact"
        renderEvent={renderEvent}
      />,
    );
    expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
    expect(screen.getByText('wait')).toBeVisible();
  });

  it.each(['pending', undefined, 'unexpected_runtime_status'])(
    'retains a task whose recorded state is %s',
    (status) => {
      const task: AgentEvent = {
        kind: 'tool_use',
        tool: 'update_plan',
        detail: 'Plan',
        sourceEventId: 3,
        input: { plan: [{ step: 'Review changes', status }] },
        resultState: 'received',
        output: ['Plan recorded.'],
      };
      render(
        <EventList
          events={[task]}
          jobId="task"
          completed
          presentation="compact"
          renderEvent={renderEvent}
        />,
      );
      expect(screen.getByText('update_plan')).toBeVisible();
      expect(screen.queryByRole('button', { name: /Show process/ })).not.toBeInTheDocument();
    },
  );

  it('keeps historical outputs within their turn boundary and outside folded process', () => {
    render(
      <HistoryTurn
        busy={false}
        presentation="compact"
        results={<div>Recorded changed files</div>}
        onOpenRun={() => undefined}
        turn={{
          key: 'history',
          jobId: 'history',
          displayNo: 1,
          state: 'done',
          messages: [],
          fallbackResponses: [],
          promptCard: 'Check the tests.',
          forkable: true,
          toolCalls: 1,
          prompt: 'Check the tests.',
          lastResponse: 'The change is ready.',
          isCurrent: false,
          events: [call, answer],
        }}
      />,
    );
    const turn = document.querySelector('[data-turn-key="history"]')!;
    expect(within(turn as HTMLElement).getByText('Check the tests.')).toBeVisible();
    expect(within(turn as HTMLElement).getByText('The change is ready.')).toBeVisible();
    const outputs = within(turn as HTMLElement).getByText('Recorded changed files');
    expect(outputs).toBeVisible();
    expect(screen.getByRole('button', { name: 'Show process (1)' })).toBeVisible();
    expect(
      outputs.compareDocumentPosition(screen.getByRole('button', { name: 'View execution' })) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
