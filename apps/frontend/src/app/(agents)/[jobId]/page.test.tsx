// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJob } from '@/components/features/agents/types';
import { useConversationReadState } from '@/components/features/agents/useConversationReadState';
const fixture = vi.hoisted(() => ({
  id: 'job-a',
  loading: false,
  job: null as AgentJob | null,
  error: null as string | null,
  reload: vi.fn(),
}));
vi.mock('next/navigation', () => ({ usePathname: () => `/${fixture.id}` }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: 'reader' } } }),
}));
vi.mock('@/components/features/agents/useAgentJobs', () => ({
  useAgentJob: () => ({
    job: fixture.job,
    loading: fixture.loading,
    error: fixture.error,
    events: [],
    reload: fixture.reload,
  }),
}));
vi.mock('@/components/features/agents/JobDetail', () => ({
  JobDetail: () => <div>Open conversation</div>,
}));
import AgentJobPage from './page';
const completed = {
  id: 'job-a',
  threadId: 'thread-a',
  state: 'done',
  currentAttemptId: 3,
  turnNo: 1,
} as AgentJob;
function ReadStatus() {
  const { isUnread } = useConversationReadState('reader');
  return (
    <span>
      {isUnread({
        key: 'thread-a',
        job: completed,
        jobIds: ['job-a'],
        archived: false,
        lastActivityAt: null,
      })
        ? 'Unread'
        : 'Read'}
    </span>
  );
}
beforeEach(() => {
  localStorage.clear();
  fixture.id = 'job-a';
  fixture.loading = false;
  fixture.error = null;
  fixture.reload.mockClear();
  fixture.job = { ...completed, state: 'running' };
});
afterEach(cleanup);
describe('viewed completion receipt', () => {
  it('marks a live completion read without any sidebar row mounted', async () => {
    const { rerender } = render(
      <>
        <ReadStatus />
        <AgentJobPage />
      </>,
    );
    expect(screen.getByText('Unread')).toBeInTheDocument();
    fixture.job = completed;
    rerender(
      <>
        <ReadStatus />
        <AgentJobPage />
      </>,
    );
    await waitFor(() => expect(screen.getByText('Read')).toBeInTheDocument());
  });
  it('does not acknowledge the previous task retained during navigation', async () => {
    fixture.id = 'job-b';
    fixture.job = completed;
    fixture.loading = true;
    render(
      <>
        <ReadStatus />
        <AgentJobPage />
      </>,
    );
    expect(screen.getByText('Unread')).toBeInTheDocument();
  });
});

describe('follow-up loading', () => {
  it('retains a mounted conversation when a background reload fails and offers a local retry', () => {
    fixture.job = completed;
    const view = render(<AgentJobPage />);
    const conversation = screen.getByText('Open conversation');
    fixture.error = 'Network failure';
    view.rerender(<AgentJobPage />);
    expect(screen.getByText('Open conversation')).toBe(conversation);
    expect(
      screen.getByText('Refresh failed — showing the last successful read.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(fixture.reload).toHaveBeenCalledOnce();
  });

  it('only shows the full loading screen when no conversation has loaded', () => {
    fixture.job = null;
    fixture.loading = true;
    render(<AgentJobPage />);
    expect(screen.getByText('Loading job…')).toBeInTheDocument();
  });

  it('keeps the mounted conversation through a slow next-turn fetch and Back', () => {
    fixture.job = completed;
    const view = render(<AgentJobPage />);
    const conversation = screen.getByText('Open conversation');
    fixture.id = 'job-b';
    fixture.loading = true;
    view.rerender(<AgentJobPage />);
    expect(screen.queryByText('Loading job…')).not.toBeInTheDocument();
    expect(screen.getByText('Open conversation')).toBe(conversation);

    fixture.job = { ...completed, id: 'job-b', turnNo: 2, state: 'queued' };
    fixture.loading = false;
    view.rerender(<AgentJobPage />);
    expect(screen.getByText('Open conversation')).toBe(conversation);

    fixture.id = completed.id;
    fixture.loading = true;
    view.rerender(<AgentJobPage />);
    expect(screen.queryByText('Loading job…')).not.toBeInTheDocument();
    fixture.job = completed;
    fixture.loading = false;
    view.rerender(<AgentJobPage />);
    expect(screen.getByText('Open conversation')).toBe(conversation);
  });
});
