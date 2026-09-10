// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJob } from '@/components/features/agents/types';
import { useConversationReadState } from '@/components/features/agents/useConversationReadState';
const fixture = vi.hoisted(() => ({ id: 'job-a', loading: false, job: null as AgentJob | null }));
vi.mock('next/navigation', () => ({ useParams: () => ({ jobId: fixture.id }) }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: 'reader' } } }),
}));
vi.mock('@/components/features/agents/useAgentJobs', () => ({
  useAgentJob: () => ({ job: fixture.job, loading: fixture.loading, error: null, events: [] }),
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
