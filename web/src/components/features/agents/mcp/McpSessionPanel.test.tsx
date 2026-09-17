// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
  status: 'pending',
  arguments_digest: 'digest',
  arguments: { query: 'release notes' },
  created_at: null,
  expires_at: '2026-09-11T12:00:00Z',
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
    expect(screen.getByText('Expires 2026-09-11T12:00:00Z')).toBeInTheDocument();

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

  it('waits to be opened before reading anything', () => {
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active={false} />);

    expect(screen.getByText('Open this panel to load tools.')).toBeInTheDocument();
    expect(mockedBindings).not.toHaveBeenCalled();
    expect(mockedInvocations).not.toHaveBeenCalled();
  });
});
