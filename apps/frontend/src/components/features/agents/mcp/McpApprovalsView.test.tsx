// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  decideMcpInvocation,
  listMcpInvocations,
  type McpInvocationApi,
  type McpInvocationsApi,
} from '@/lib/api/mcp';
import { PUSH_BATCH_MS, receiveHint, resetAgentPush, setAgentPushLive } from '../agentPush';
import { McpApprovalsProvider } from './McpApprovalsProvider';
import { McpApprovalsLink, McpApprovalsView } from './McpApprovalsView';

vi.mock('@/lib/api/mcp', async (original) => ({
  ...(await original<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn(),
  decideMcpInvocation: vi.fn(),
}));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});

const list = vi.mocked(listMcpInvocations);
const decide = vi.mocked(decideMcpInvocation);
const call: McpInvocationApi = {
  id: 11,
  job_id: 'hidden-job',
  thread_id: 'thread-1',
  thread_title: 'Background task',
  connection_id: 3,
  connection_label: 'Team wiki',
  capability_name: 'search',
  capability_kind: 'tool',
  capability_description: 'Search pages',
  capability_annotations: { readOnlyHint: true },
  status: 'pending',
  arguments_digest: 'digest',
  arguments: { query: 'private release notes' },
  created_at: null,
  expires_at: null,
  decided_at: null,
  result_summary: null,
  guidance: '',
};
function page(invocations: McpInvocationApi[], next: number | null = null): McpInvocationsApi {
  return { invocations, has_more: next != null, next_before_id: next };
}
function App({ ownerId = 'owner-1', enabled = true }: { ownerId?: string; enabled?: boolean }) {
  return (
    <McpApprovalsProvider ownerId={ownerId} enabled={enabled}>
      <McpApprovalsLink current={false} className="" />
      <McpApprovalsView />
    </McpApprovalsProvider>
  );
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  list.mockReset().mockResolvedValue(page([]));
  decide.mockReset();
});
afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.useRealTimers();
});

describe('owner tool approvals', () => {
  it('shows background conversation requests, exact arguments, and one-time decisions', async () => {
    list.mockResolvedValue(page([call]));
    decide.mockResolvedValue({ ...call, status: 'approved' });
    render(<App />);
    expect(await screen.findByRole('link', { name: 'Approvals (1 pending)' })).toHaveAttribute(
      'href',
      '/approvals',
    );
    expect(screen.getByText('Background task')).toBeInTheDocument();
    expect(screen.getByText('Team wiki wants to call search')).toBeInTheDocument();
    expect(screen.getByText(/private release notes/)).toBeInTheDocument();
    expect(screen.getByText('Server hints: Read-only')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open conversation' })).toHaveAttribute(
      'href',
      '/hidden-job',
    );
    expect(screen.queryByRole('button', { name: 'Always allow' })).not.toBeInTheDocument();
    expect(list).toHaveBeenCalledWith({ pendingOnly: true, limit: 50 });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('status')).toHaveTextContent('search · approved');
    expect(decide).toHaveBeenCalledWith(11, true);
    // Even an old read that still contains this call cannot restore its button.
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('uses a lower-bound count and loads older pending approvals with a cursor', async () => {
    const head = Array.from({ length: 50 }, (_, index) => ({
      ...call,
      id: 100 - index,
      capability_name: `request_${100 - index}`,
    }));
    list.mockImplementation(async ({ beforeId }) =>
      beforeId === 51
        ? page([{ ...call, id: 50, capability_name: 'older_request' }])
        : page(head, 51),
    );
    render(<App />);
    expect(
      await screen.findByRole('link', { name: 'Approvals (50+ pending)' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more approvals' }));
    expect(await screen.findByText('Team wiki wants to call older_request')).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith({ pendingOnly: true, limit: 50, beforeId: 51 });
    expect(screen.getByRole('link', { name: 'Approvals (51 pending)' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more approvals' })).not.toBeInTheDocument();
  });

  it('refreshes older loaded pages so expired approvals disappear', async () => {
    let oldPresent = true;
    list.mockImplementation(async ({ beforeId }) =>
      beforeId === 11
        ? page(oldPresent ? [{ ...call, id: 10, capability_name: 'older_request' }] : [])
        : page([call], 11),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more approvals' }));
    await screen.findByText('Team wiki wants to call older_request');
    oldPresent = false;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(screen.queryByText('Team wiki wants to call older_request')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Team wiki wants to call search')).toBeInTheDocument();
  });

  it('does not restore a decision while a later pending page is still loading', async () => {
    const older = deferred<McpInvocationsApi>();
    let holdOlder = true;
    list.mockImplementation(async ({ beforeId }) =>
      beforeId === 11 ? (holdOlder ? older.promise : page([])) : page([call], 11),
    );
    decide.mockResolvedValue({ ...call, status: 'approved' });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more approvals' }));
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith({ pendingOnly: true, limit: 50, beforeId: 11 }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument(),
    );
    holdOlder = false;
    await act(async () => older.resolve(page([])));
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(screen.getByText('search · approved')).toBeInTheDocument();
  });

  it('keeps each concurrent decision busy until its own response arrives', async () => {
    const first = deferred<McpInvocationApi>();
    const second = deferred<McpInvocationApi>();
    const other = { ...call, id: 12, thread_title: 'Other task', capability_name: 'update' };
    list.mockResolvedValue(page([call, other]));
    decide.mockImplementation((id) => (id === 11 ? first.promise : second.promise));
    render(<App />);
    const a = await screen.findByRole('region', { name: 'Background task' });
    const b = screen.getByRole('region', { name: 'Other task' });
    fireEvent.click(within(a).getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(b).getByRole('button', { name: 'Deny' }));
    expect(within(a).getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(within(b).getByRole('button', { name: 'Deny' })).toBeDisabled();
    await act(async () => first.reject(new Error('Please retry the first decision.')));
    expect(within(a).getByRole('alert')).toHaveTextContent('Please retry the first decision.');
    expect(within(a).getByRole('button', { name: 'Approve' })).toBeEnabled();
    expect(within(b).getByRole('button', { name: 'Deny' })).toBeDisabled();
    await act(async () => second.resolve({ ...other, status: 'denied' }));
    expect(decide).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('region', { name: 'Other task' })).not.toBeInTheDocument();
  });

  it('keeps the queue through a read failure and allows a refresh retry', async () => {
    list.mockResolvedValue(page([call]));
    render(<App />);
    await screen.findByText('Team wiki wants to call search');
    list.mockRejectedValue(new Error('Temporary connection failure.'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Temporary connection failure.');
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
    list.mockResolvedValue(page([]));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(
      await screen.findByText('No tool requests are waiting for your approval.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows a lost decision race even after the pending card disappears', async () => {
    list.mockResolvedValueOnce(page([call])).mockResolvedValue(page([]));
    decide.mockRejectedValue(new Error('This request has already expired.'));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('alert')).toHaveTextContent('This request has already expired.');
  });

  it('refreshes the queue and count at the approval deadline without waiting for a push', async () => {
    vi.useFakeTimers();
    setAgentPushLive(true);
    list
      .mockResolvedValueOnce(
        page([{ ...call, expires_at: new Date(Date.now() + 1000).toISOString() }]),
      )
      .mockResolvedValue(page([]));
    await act(async () => render(<App />));
    expect(screen.getByRole('link', { name: 'Approvals (1 pending)' })).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(1005));
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('link', { name: 'Approvals (1 pending)' })).not.toBeInTheDocument();
    expect(screen.getByText('No tool requests are waiting for your approval.')).toBeInTheDocument();
  });

  it('clears decrypted bodies synchronously when switching owners', async () => {
    list.mockResolvedValueOnce(page([call]));
    const { rerender } = render(<App />);
    await screen.findByText(/private release notes/);
    const next = deferred<McpInvocationsApi>();
    list.mockReturnValueOnce(next.promise);
    rerender(<App ownerId="owner-2" />);
    expect(screen.queryByText(/private release notes/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Approvals (1 pending)' })).not.toBeInTheDocument();
    await act(async () => next.resolve(page([])));
    expect(screen.getByText('No tool requests are waiting for your approval.')).toBeInTheDocument();
  });

  it('discards old account reads and decisions after switching owners', async () => {
    const old = deferred<McpInvocationsApi>();
    list.mockReturnValueOnce(old.promise).mockResolvedValue(page([]));
    const { rerender } = render(<App />);
    rerender(<App ownerId="owner-2" />);
    await act(async () => old.resolve(page([call])));
    expect(screen.queryByText(/private release notes/)).not.toBeInTheDocument();
    expect(screen.getByText('No tool requests are waiting for your approval.')).toBeInTheDocument();

    list.mockResolvedValue(page([call]));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    const decision = deferred<McpInvocationApi>();
    decide.mockReturnValueOnce(decision.promise);
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    list.mockResolvedValue(page([]));
    rerender(<App ownerId="owner-3" />);
    await act(async () => decision.resolve({ ...call, status: 'approved' }));
    expect(screen.queryByText('search · approved')).not.toBeInTheDocument();
    expect(screen.queryByText(/private release notes/)).not.toBeInTheDocument();
  });

  it('does not read approvals when admission is unavailable and stops after it is revoked', async () => {
    vi.useFakeTimers();
    list.mockResolvedValue(page([call]));
    const { rerender } = render(<App enabled={false} />);
    expect(list).not.toHaveBeenCalled();
    await act(async () => rerender(<App enabled />));
    expect(screen.getByText(/private release notes/)).toBeInTheDocument();
    rerender(<App enabled={false} />);
    expect(screen.queryByText(/private release notes/)).not.toBeInTheDocument();
    list.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(list).not.toHaveBeenCalled();
  });

  it('reads background changes immediately while push keeps polling slow', async () => {
    vi.useFakeTimers();
    setAgentPushLive(true);
    await act(async () => render(<App />));
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(list).toHaveBeenCalledTimes(1);
    list.mockResolvedValue(page([call]));
    await act(async () => {
      receiveHint('mcp', 'hidden-job');
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('link', { name: 'Approvals (1 pending)' })).toBeInTheDocument();
  });
});
