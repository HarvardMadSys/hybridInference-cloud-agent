// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { EventRow, ThreadTurn } from './ChatMessages';
import type { AgentThreadMessage, GoalTurn } from './types';

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
