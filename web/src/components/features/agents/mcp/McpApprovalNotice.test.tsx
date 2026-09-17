// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { listMcpInvocations } from '@/lib/api/mcp';
import { McpApprovalNotice } from './McpApprovalNotice';

vi.mock('@/lib/api/mcp', () => ({ listMcpInvocations: vi.fn() }));
const list = vi.mocked(listMcpInvocations);

beforeEach(() => {
  vi.useFakeTimers();
  list.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('shows a pending approval beside the conversation and opens its tools pane', async () => {
  list.mockResolvedValue({ invocations: [{ status: 'pending' }] } as Awaited<
    ReturnType<typeof list>
  >);
  const onReview = vi.fn();
  await act(async () => {
    render(<McpApprovalNotice jobId="job-1" active onReview={onReview} />);
  });
  expect(screen.getByRole('status')).toHaveTextContent('A tool call needs your approval.');
  expect(list).toHaveBeenCalledWith({ jobId: 'job-1', pendingOnly: true });
  fireEvent.click(screen.getByRole('button', { name: 'Review tools' }));
  expect(onReview).toHaveBeenCalledOnce();
  list.mockResolvedValue({ invocations: [] });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('does not show a stale decision after switching jobs', async () => {
  let finish!: (value: Awaited<ReturnType<typeof list>>) => void;
  list.mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const { rerender } = render(<McpApprovalNotice jobId="old-job" active onReview={() => {}} />);
  list.mockResolvedValue({ invocations: [] });
  await act(async () => {
    rerender(<McpApprovalNotice jobId="new-job" active onReview={() => {}} />);
  });
  await act(async () => {
    finish({ invocations: [{ status: 'pending' }] } as Awaited<ReturnType<typeof list>>);
  });
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('stops polling when the job is no longer active', async () => {
  list.mockResolvedValue({ invocations: [] });
  const { rerender } = render(<McpApprovalNotice jobId="job-1" active onReview={() => {}} />);
  await act(async () => {
    rerender(<McpApprovalNotice jobId="job-1" active={false} onReview={() => {}} />);
  });
  list.mockClear();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(9000);
  });
  expect(list).not.toHaveBeenCalled();
});
