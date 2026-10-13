// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Highlight, TaskRow } from './SidebarRows';
import type { AgentJob } from './types';

const mocks = vi.hoisted(() => ({ review: vi.fn() }));
vi.mock('./useSessionReview', () => ({ useSessionReview: mocks.review }));

const JOB: AgentJob = {
  id: 'job-1',
  title: 'Initial request',
  state: 'done',
  repo: 'owner/repo',
  baseSha: 'abcdef1',
  branch: 'agent/job-1',
  runtime: 'claude-code',
  model: 'model',
  spentUsd: 0,
  timeoutLabel: '',
  sandbox: '',
  attempts: [],
  events: [],
  diffFiles: [],
  gates: [],
  usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: 0 },
};
beforeEach(() => mocks.review.mockReturnValue(null));
afterEach(cleanup);
describe('literal search highlighting', () => {
  it('keeps original offsets after Unicode letters whose lowercase form changes length', () => {
    const { container } = render(<Highlight text="İstanbul deploy — DEPLOY" query="deploy" />);
    expect(container.textContent).toBe('İstanbul deploy — DEPLOY');
    expect([...container.querySelectorAll('mark')].map((mark) => mark.textContent)).toEqual([
      'deploy',
      'DEPLOY',
    ]);
  });
  it('treats metacharacters and HTML as literal text', () => {
    const text = 'a.*[x](y)$^?+|\\ <img src=x>';
    const { container, rerender } = render(<Highlight text={text} query={'.*[x](y)$^?+|\\'} />);
    expect(container.textContent).toBe(text);
    expect(container.querySelector('mark')?.textContent).toBe('.*[x](y)$^?+|\\');
    rerender(<Highlight text={text} query="<img src=x>" />);
    expect(container.querySelector('mark')?.textContent).toBe('<img src=x>');
    expect(container.querySelector('img')).toBeNull();
  });
  it('preserves surrogate pairs before and within a match', () => {
    const { container } = render(<Highlight text="🚀 build 🚀 deploy" query="🚀 deploy" />);
    expect(container.textContent).toBe('🚀 build 🚀 deploy');
    expect(container.querySelector('mark')?.textContent).toBe('🚀 deploy');
  });
});

describe('conversation status in sidebar rows', () => {
  it.each([
    ['queued', undefined, 'Queued'],
    ['running', undefined, 'Running'],
    ['failed', undefined, 'Failed'],
    ['done', undefined, 'Idle'],
    ['needs_review', undefined, 'Idle'],
    ['cancelled', undefined, 'Idle'],
    ['cancelled', 'running', 'Running'],
    ['done', 'queued', 'Queued'],
    ['queued', 'running', 'Running'],
  ] as const)(
    'shows %s in a %s chat as %s and preserves unread',
    (state, conversationState, label) => {
      const inProgress = label === 'Running' || label === 'Queued';
      render(
        <TaskRow
          row={{
            key: 'conversation-1',
            job: { ...JOB, state, conversationState },
            jobIds: [JOB.id],
            archived: false,
            lastActivityAt: null,
          }}
          activeKey={null}
          isUnread={() => true}
          onRead={vi.fn()}
          archiving={null}
          pinning={null}
          deleting={null}
          onArchive={vi.fn()}
          onSetPinned={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
      expect(screen.getByLabelText(label)).toHaveAttribute('title', label);
      if (label === 'Running') expect(screen.getByLabelText(label)).toHaveClass('animate-spin');
      else expect(screen.getByLabelText(label)).not.toHaveClass('animate-spin');
      if (inProgress) expect(screen.queryByLabelText('Unread result')).not.toBeInTheDocument();
      else expect(screen.getByLabelText('Unread result')).toBeInTheDocument();
    },
  );
});

describe('sessions waiting for human review', () => {
  it.each([
    ['question', 'Waiting for your answer'],
    ['approval', 'Waiting for your tool approval'],
  ] as const)('keeps a selected %s marker visible after opening the session', (review, reason) => {
    mocks.review.mockReturnValue(review);
    const row = {
      key: 'conversation-1',
      job: { ...JOB, state: 'running' as const, threadId: 'thread-1', currentAttemptId: 5 },
      jobIds: [JOB.id],
      archived: false,
      lastActivityAt: null,
    };
    const onRead = vi.fn();
    const props = {
      row,
      activeKey: row.key,
      isUnread: () => true,
      onRead,
      archiving: null,
      pinning: null,
      deleting: null,
      onArchive: vi.fn(),
      onSetPinned: vi.fn(),
      onDelete: vi.fn(),
    };
    const { rerender } = render(<TaskRow {...props} />);
    const marker = screen.getByRole('status', { name: reason });
    expect(marker).toBeVisible();
    expect(marker).toHaveTextContent('Needs review');
    expect(marker).toHaveAttribute('title', reason);
    expect(screen.getByRole('link', { name: JOB.title })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByLabelText('Unread result')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('link', { name: JOB.title }));
    expect(onRead).toHaveBeenCalledWith(row);
    rerender(<TaskRow {...props} isUnread={() => false} />);
    expect(screen.getByRole('status', { name: reason })).toBeVisible();

    mocks.review.mockReturnValue(null);
    rerender(<TaskRow {...props} isUnread={() => false} />);
    expect(screen.queryByRole('status', { name: reason })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Running')).toHaveClass('animate-spin');
  });
});
