// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { receiveHint } from '../agentPush';
import { McpSessionPanel } from './McpSessionPanel';
import type { McpBindingApi, McpInvocationApi } from '@/lib/api/mcp';

vi.mock('@/lib/api/mcp', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/mcp')>();
  return {
    ...actual,
    getMcpThreadBindings: vi.fn(),
    listMcpInvocations: vi.fn(),
    updateMcpThreadBinding: vi.fn(),
    decideMcpInvocation: vi.fn(),
  };
});

import {
  decideMcpInvocation,
  getMcpThreadBindings,
  listMcpInvocations,
  updateMcpThreadBinding,
} from '@/lib/api/mcp';

const mockedBindings = getMcpThreadBindings as unknown as ReturnType<typeof vi.fn>;
const mockedInvocations = listMcpInvocations as unknown as ReturnType<typeof vi.fn>;
const mockedUpdate = updateMcpThreadBinding as unknown as ReturnType<typeof vi.fn>;
const mockedDecide = decideMcpInvocation as unknown as ReturnType<typeof vi.fn>;

const binding: McpBindingApi = {
  connection_id: 3,
  connection_label: 'Team wiki',
  alias: 'wiki',
  enabled: true,
  selection_pending: true,
  available: true,
  error: null,
  capabilities: [
    {
      kind: 'tool',
      name: 'search',
      description: 'Search pages',
      input_schema: null,
      annotations: {},
    },
    { kind: 'prompt', name: 'summarize', description: '', input_schema: null, annotations: {} },
    {
      kind: 'resource_template',
      name: 'page://{slug}',
      description: '',
      input_schema: null,
      annotations: {},
    },
  ],
  tools: {},
  resources: {},
  prompts: {},
  config_revision: 1,
  authorization_epoch: 1,
  capability_revision: 1,
  policy_revision: 1,
};

const unavailable: McpBindingApi = {
  ...binding,
  connection_id: 4,
  connection_label: 'Old server',
  alias: 'old',
  selection_pending: false,
  available: false,
  error: 'the credential expired',
  capabilities: [],
};

const invocation: McpInvocationApi = {
  id: 11,
  job_id: 'job-1',
  thread_id: 'thread-1',
  connection_id: 3,
  connection_label: 'Team wiki',
  capability_name: 'search',
  capability_kind: 'tool',
  capability_description: null,
  capability_annotations: {},
  status: 'pending',
  arguments_digest: 'digest',
  arguments: { query: 'release notes' },
  created_at: null,
  expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  decided_at: null,
  result_summary: null,
  guidance: '',
};

beforeEach(() => {
  mockedBindings.mockResolvedValue({ bindings: [binding] });
  mockedInvocations.mockResolvedValue({ invocations: [] });
  mockedUpdate.mockImplementation(async (_thread, request) => ({
    bindings: [{ ...binding, ...request, selection_pending: request.use_defaults }],
  }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe('McpSessionPanel', () => {
  it('shows inline approvals without loading server settings or filling the chat with history', async () => {
    mockedBindings.mockRejectedValue(new Error('Server settings unavailable'));
    mockedInvocations.mockImplementation(({ pendingOnly }) =>
      Promise.resolve({
        invocations: pendingOnly
          ? [invocation]
          : [{ ...invocation, id: 12, capability_name: 'older_call', status: 'completed' }],
      }),
    );
    mockedDecide.mockResolvedValue({ ...invocation, status: 'denied' });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);
    expect(await screen.findByText('Team wiki wants to call search')).toBeInTheDocument();
    expect(mockedBindings).not.toHaveBeenCalled();
    expect(screen.queryByText('MCP servers')).not.toBeInTheDocument();
    expect(screen.queryByText('older_call · completed')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    expect(await screen.findByText('search · denied')).toBeInTheDocument();
    expect(mockedDecide).toHaveBeenCalledWith(11, false);
  });

  it('shows a failed inline decision where the user can retry it', async () => {
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    mockedDecide.mockRejectedValue(new Error('Please retry this decision.'));
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Please retry this decision.');
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
  });

  it('shows connected servers automatically without an attach step or save requirement', async () => {
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    expect(await screen.findByRole('switch', { name: /Use Team wiki/ })).toBeChecked();
    expect(screen.queryByRole('button', { name: /Attach/ })).not.toBeInTheDocument();
    expect(mockedUpdate).not.toHaveBeenCalled();
    expect(screen.getByText('Tools and permissions').closest('details')).not.toHaveAttribute(
      'open',
    );
  });

  it('persists a conversation opt-out immediately while preserving its custom rules', async () => {
    mockedBindings.mockResolvedValue({
      bindings: [{ ...binding, selection_pending: false, tools: { search: 'allow' } }],
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByRole('switch', { name: /Use Team wiki/ }));
    await waitFor(() =>
      expect(mockedUpdate).toHaveBeenCalledWith(
        'thread-1',
        expect.objectContaining({
          enabled: false,
          tools: { search: 'allow' },
          use_defaults: false,
        }),
      ),
    );
    expect(await screen.findByText('Off for this conversation')).toBeInTheDocument();
  });

  it('enables a previously empty selection with confirmation required', async () => {
    mockedBindings.mockResolvedValue({
      bindings: [{ ...binding, enabled: false, selection_pending: false }],
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByRole('switch', { name: /Use Team wiki/ }));
    await waitFor(() =>
      expect(mockedUpdate).toHaveBeenCalledWith(
        'thread-1',
        expect.objectContaining({ enabled: true, use_defaults: true, tools: {} }),
      ),
    );
  });

  it('saves tool, prompt and resource permissions for only the edited server', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding, unavailable] });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByText('Tools and permissions'));
    const group = screen.getByRole('radiogroup', { name: 'Policy for search' });
    expect(within(group).getByRole('radio', { name: 'Confirm' })).toBeChecked();
    fireEvent.click(within(group).getByRole('radio', { name: 'Allow' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save permissions' }));
    await waitFor(() =>
      expect(mockedUpdate).toHaveBeenCalledWith('thread-1', {
        connection_id: 3,
        alias: 'wiki',
        enabled: true,
        use_defaults: true,
        tools: { search: 'allow' },
        resources: { 'page://{slug}': 'confirm' },
        prompts: { summarize: 'confirm' },
      }),
    );
  });

  it('resets explicit permissions to ask without enabling a disabled server', async () => {
    mockedBindings.mockResolvedValue({
      bindings: [
        { ...binding, enabled: false, selection_pending: false, tools: { search: 'allow' } },
      ],
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByText('Tools and permissions'));
    fireEvent.click(screen.getByRole('button', { name: 'Reset to ask before each call' }));
    await waitFor(() =>
      expect(mockedUpdate).toHaveBeenCalledWith(
        'thread-1',
        expect.objectContaining({
          enabled: false,
          use_defaults: true,
          tools: {},
          resources: {},
          prompts: {},
        }),
      ),
    );
  });

  it('protects pending saves and does not overwrite them when reopening the panel', async () => {
    let finish!: (value: { bindings: McpBindingApi[] }) => void;
    mockedUpdate.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const { rerender } = render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByRole('switch', { name: /Use Team wiki/ }));
    expect(screen.getByRole('switch')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
    rerender(<McpSessionPanel jobId="job-1" threadId="thread-1" active={false} />);
    rerender(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    expect(mockedBindings).toHaveBeenCalledTimes(1);
    await act(async () => {
      finish({ bindings: [{ ...binding, enabled: false }] });
    });
    expect(screen.getByRole('switch')).not.toBeChecked();
    expect(screen.getByRole('switch')).toBeEnabled();
  });

  it('keeps the saved switch state after a failed update', async () => {
    mockedUpdate.mockRejectedValue(new Error('Could not save'));
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByRole('switch', { name: /Use Team wiki/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save');
    expect(screen.getByRole('switch')).toBeChecked();
  });

  it('shows an actionable connection error alongside healthy servers', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding, { ...unavailable, enabled: true }] });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    expect(await screen.findByText('Needs attention')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Manage connection' })).toHaveAttribute(
      'href',
      '/integrations',
    );
    expect(screen.getByRole('switch', { name: /Use Team wiki/ })).toBeChecked();
  });

  it('offers a direct setup link when there are no servers', async () => {
    mockedBindings.mockResolvedValue({ bindings: [] });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    expect(await screen.findByRole('link', { name: 'Add MCP server' })).toHaveAttribute(
      'href',
      '/integrations',
    );
  });
  it('follows an approved call to its final outcome without reviving stale approvals', async () => {
    vi.useFakeTimers();
    mockedBindings.mockResolvedValue({ bindings: [] });
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    mockedDecide.mockResolvedValue({ ...invocation, status: 'approved' });
    await act(async () => {
      render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    });

    let finishPoll!: (value: { invocations: McpInvocationApi[] }) => void;
    mockedInvocations.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishPoll = resolve;
        }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    });
    expect(screen.getByText('search · approved')).toBeInTheDocument();
    await act(async () => {
      finishPoll({ invocations: [invocation] });
    });
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();

    mockedInvocations.mockResolvedValue({
      invocations: [
        {
          ...invocation,
          status: 'outcome_unknown',
          guidance: 'Check the service before retrying.',
        },
      ],
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByText('search · outcome_unknown')).toBeInTheDocument();
    expect(screen.getByText('Check the service before retrying.')).toBeInTheDocument();
    expect(mockedInvocations).toHaveBeenCalledWith({ jobId: 'job-1' });
    expect(mockedInvocations).toHaveBeenCalledWith({ jobId: 'job-1', pendingOnly: true });
  });

  it('keeps older pending approvals visible even when recent history is full', async () => {
    mockedBindings.mockResolvedValue({ bindings: [] });
    const history = Array.from({ length: 50 }, (_, index) => ({
      ...invocation,
      id: 100 + index,
      capability_name: `finished_${index}`,
      status: 'completed',
    }));
    mockedInvocations.mockImplementation(({ pendingOnly }) =>
      Promise.resolve({
        invocations: pendingOnly ? [invocation] : history,
      }),
    );
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    expect(await screen.findByText('Team wiki wants to call search')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('clears the previous conversation while the next conversation loads', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding] });
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    const { rerender } = render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    await screen.findByText('Team wiki wants to call search');

    mockedBindings.mockImplementation(() => new Promise(() => {}));
    rerender(<McpSessionPanel jobId="job-2" threadId="thread-2" active />);
    expect(screen.queryByText('Team wiki wants to call search')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save permissions' })).not.toBeInTheDocument();
  });

  it('renders a pending call with its arguments and records the decision', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding] });
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    mockedDecide.mockResolvedValue({
      ...invocation,
      status: 'approved',
      decided_at: '2026-09-11T11:00:00Z',
      guidance: '',
    });

    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);

    expect(await screen.findByText('Team wiki wants to call search')).toBeInTheDocument();
    expect(screen.getByText(/"query": "release notes"/)).toBeInTheDocument();
    expect(screen.getByText(/Expires in/)).toHaveAttribute('dateTime', invocation.expires_at);

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(mockedDecide).toHaveBeenCalledWith(11, true));
    // The decided call stays visible with the state it reached, because its
    // guidance is the only place the outcome is explained.
    expect(await screen.findByText('search · approved')).toBeInTheDocument();
  });

  it('shows guidance for a call that can no longer be decided', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding] });
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    mockedDecide.mockResolvedValue({
      ...invocation,
      status: 'expired',
      guidance: 'This approval expired before it was used. Ask the agent again to re-request it.',
    });

    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    await screen.findByText('Team wiki wants to call search');
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));

    expect(
      await screen.findByText(
        'This approval expired before it was used. Ask the agent again to re-request it.',
      ),
    ).toBeInTheDocument();
  });

  it('explains what a held call does before asking for a decision', async () => {
    const now = Date.now();
    mockedInvocations.mockResolvedValue({
      invocations: [
        {
          ...invocation,
          capability_name: 'list_traces',
          capability_description: 'List recent traces for a service.',
          capability_annotations: { title: 'List traces', readOnlyHint: true, openWorldHint: true },
          arguments: {},
          created_at: new Date(now - 2 * 60_000 - 5_000).toISOString(),
          expires_at: new Date(now + 5 * 60_000).toISOString(),
        },
      ],
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);

    expect(await screen.findByText('Team wiki wants to call list_traces')).toBeInTheDocument();
    expect(screen.getByText('List traces')).toBeInTheDocument();
    expect(screen.getByText('List recent traces for a service.')).toBeInTheDocument();
    expect(
      screen.getByText('Server hints: Read-only · Reaches external services'),
    ).toBeInTheDocument();
    expect(screen.getByText('Requested 2 min ago')).toBeInTheDocument();
    expect(screen.getByText('Expires in 5 min')).toBeInTheDocument();
    expect(screen.getByText('No arguments.')).toBeInTheDocument();
  });

  it('names what a resource read or prompt would do', async () => {
    mockedInvocations.mockResolvedValue({
      invocations: [
        { ...invocation, id: 21, capability_kind: 'resource', capability_name: 'page://home' },
        { ...invocation, id: 22, capability_kind: 'prompt', capability_name: 'summarize' },
      ],
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);

    expect(await screen.findByText('Team wiki wants to read page://home')).toBeInTheDocument();
    expect(screen.getByText('Team wiki wants to use the prompt summarize')).toBeInTheDocument();
  });

  it('records Always allow in the approval request before the page closes', async () => {
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    let finish!: (value: McpInvocationApi) => void;
    mockedDecide.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const { unmount } = render(
      <McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Always allow' }));
    await waitFor(() => expect(mockedDecide).toHaveBeenCalledWith(11, true, true));
    unmount();
    await act(async () =>
      finish({ ...invocation, status: 'approved', always_allow_status: 'pending' }),
    );
    expect(mockedUpdate).not.toHaveBeenCalled();
  });

  it('shows the saved intent while the server waits for other held calls', async () => {
    mockedInvocations.mockResolvedValue({ invocations: [invocation] });
    mockedDecide.mockResolvedValue({
      ...invocation,
      status: 'approved',
      always_allow_status: 'pending',
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);
    fireEvent.click(await screen.findByRole('button', { name: 'Always allow' }));
    expect(await screen.findByText(/Always allow was recorded/)).toHaveTextContent(
      'you can leave this page',
    );
    expect(mockedUpdate).not.toHaveBeenCalled();
  });

  it.each([
    ['applied', 'Always allow was saved for this conversation.'],
    ['discarded', 'Always allow was not applied because the connection or permissions changed.'],
  ] as const)(
    'loads the server-owned %s permission outcome after reopening',
    async (status, text) => {
      const calls = [{ ...invocation, status: 'completed' as const, always_allow_status: status }];
      mockedInvocations.mockImplementation(({ pendingOnly }) =>
        Promise.resolve({ invocations: pendingOnly ? [] : calls }),
      );
      render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
      expect(await screen.findByText(text, { exact: false })).toBeInTheDocument();
      expect(mockedUpdate).not.toHaveBeenCalled();
    },
  );

  it.each(['allow', 'deny'] as const)(
    'refreshes the current %s rule after a saved intent while preserving other draft edits',
    async (policy) => {
      let calls: McpInvocationApi[] = [];
      mockedInvocations.mockImplementation(({ pendingOnly }) =>
        Promise.resolve({
          invocations: pendingOnly ? [] : calls,
        }),
      );
      render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
      const prompt = await screen.findByRole('radiogroup', { name: 'Policy for summarize' });
      fireEvent.click(within(prompt).getByRole('radio', { name: 'Deny' }));
      mockedBindings.mockResolvedValue({ bindings: [{ ...binding, tools: { search: policy } }] });
      calls = [{ ...invocation, status: 'completed', always_allow_status: 'applied' }];
      receiveHint('mcp', 'job-1');
      await waitFor(() =>
        expect(
          within(screen.getByRole('radiogroup', { name: 'Policy for search' })).getByRole('radio', {
            name: policy === 'allow' ? 'Allow' : 'Deny',
          }),
        ).toBeChecked(),
      );
      expect(within(prompt).getByRole('radio', { name: 'Deny' })).toBeChecked();
    },
  );

  it('keeps two simultaneous decisions busy independently', async () => {
    const other = { ...invocation, id: 12, capability_name: 'other' };
    mockedInvocations.mockResolvedValue({ invocations: [invocation, other] });
    let finishFirst!: (value: McpInvocationApi) => void;
    let finishSecond!: (value: McpInvocationApi) => void;
    mockedDecide.mockImplementation(
      (id: number) =>
        new Promise((resolve) => {
          if (id === 11) finishFirst = resolve;
          else finishSecond = resolve;
        }),
    );
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);
    const a = (await screen.findByText('Team wiki wants to call search')).parentElement!;
    const b = screen.getByText('Team wiki wants to call other').parentElement!;
    fireEvent.click(within(a).getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(b).getByRole('button', { name: 'Deny' }));
    expect(within(a).getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(within(b).getByRole('button', { name: 'Deny' })).toBeDisabled();
    await act(async () => finishFirst({ ...invocation, status: 'approved' }));
    expect(within(b).getByRole('button', { name: 'Deny' })).toBeDisabled();
    await act(async () => finishSecond({ ...other, status: 'denied' }));
    expect(mockedDecide).toHaveBeenCalledTimes(2);
  });

  it('loads approval requests beyond the first pending page', async () => {
    mockedInvocations.mockImplementation(async ({ pendingOnly, beforeId }) => {
      if (!pendingOnly) return { invocations: [] };
      if (beforeId === 11) {
        return {
          invocations: [{ ...invocation, id: 10, capability_name: 'older_call' }],
          has_more: false,
          next_before_id: null,
        };
      }
      return { invocations: [invocation], has_more: true, next_before_id: 11 };
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);
    expect(await screen.findByText('Team wiki wants to call older_call')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(2);
    expect(mockedInvocations).toHaveBeenCalledWith({
      jobId: 'job-1',
      pendingOnly: true,
      beforeId: 11,
    });
  });

  it('loads older history and preserves its depth on refresh', async () => {
    const history = Array.from({ length: 50 }, (_, index) => ({
      ...invocation,
      id: 100 - index,
      capability_name: `recent_${index}`,
      status: 'completed',
    }));
    mockedInvocations.mockImplementation(async ({ pendingOnly, beforeId }) => {
      if (pendingOnly) return { invocations: [] };
      if (beforeId === 51) {
        return {
          invocations: [{ ...invocation, id: 50, capability_name: 'older_call', status: 'denied' }],
          has_more: false,
          next_before_id: null,
        };
      }
      return { invocations: history, has_more: true, next_before_id: 51 };
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load older requests' }));
    expect(await screen.findByText('older_call · denied')).toBeInTheDocument();
    expect(mockedInvocations).toHaveBeenCalledWith({ jobId: 'job-1', beforeId: 51 });
    expect(screen.queryByRole('button', { name: 'Load older requests' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockedInvocations.mock.calls.filter(([query]) => query.beforeId === 51)).toHaveLength(
        2,
      ),
    );
    expect(screen.getByText('older_call · denied')).toBeInTheDocument();
  });

  it('keeps a newer pending snapshot when the initial history read finishes late', async () => {
    let finishHistory!: (value: { invocations: McpInvocationApi[] }) => void;
    let reads = 0;
    mockedInvocations.mockImplementation(({ pendingOnly }) => {
      reads += 1;
      if (reads === 1)
        return new Promise((resolve) => {
          finishHistory = resolve;
        });
      return Promise.resolve({ invocations: pendingOnly && reads > 2 ? [invocation] : [] });
    });
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active approvalsOnly />);
    receiveHint('mcp', 'job-1');
    expect(await screen.findByText('Team wiki wants to call search')).toBeInTheDocument();
    await act(async () => finishHistory({ invocations: [] }));
    expect(screen.getByText('Team wiki wants to call search')).toBeInTheDocument();
  });

  it('waits to be opened before reading anything', () => {
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active={false} />);

    expect(screen.getByText('Open this panel to load tools.')).toBeInTheDocument();
    expect(mockedBindings).not.toHaveBeenCalled();
    expect(mockedInvocations).not.toHaveBeenCalled();
  });
});
