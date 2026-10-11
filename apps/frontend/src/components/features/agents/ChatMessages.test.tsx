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
    expect(screen.getByText('Started in background.')).not.toBeVisible();
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
    expect(screen.getByText('2 passed')).not.toBeVisible();
    expect(screen.getByText('router source')).not.toBeVisible();

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
    expect(screen.getByText('2 passed')).not.toBeVisible();
    expect(screen.getByText('router source')).not.toBeVisible();
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
    const toolAnchor = document.querySelector('[data-block-key="done:e1"]');
    const answerAnchor = document.querySelector('[data-block-key="done:e2"]');
    expect(toolAnchor).not.toBeVisible();
    expect(screen.getByText('The change is ready.')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Show process (1)' }));
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
    expect(screen.getByText('Bash: npm test')).toBeVisible();
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
    expect(screen.getByText('Checks passed')).not.toBeVisible();
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
    expect(screen.getByText('Bash: npm test')).not.toBeVisible();
    expect(screen.getByRole('button', { name: 'Show process (1)' })).toBeVisible();
    expect(screen.getByText('The change is ready.')).toBeVisible();
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
    expect(screen.getByText('wait: Wait for reviewer')).toBeVisible();
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
      expect(screen.getByText('update_plan: Plan')).toBeVisible();
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
