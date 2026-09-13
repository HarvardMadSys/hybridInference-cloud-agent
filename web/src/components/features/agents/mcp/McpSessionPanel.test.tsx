// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { McpSessionPanel } from './McpSessionPanel';
import type { McpBindingApi, McpConnectionApi, McpInvocationApi } from '@/lib/api/mcp';

vi.mock('@/lib/api/mcp', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/mcp')>();
  return {
    ...actual,
    getMcpConnectionCapabilities: vi.fn(),
    getMcpThreadBindings: vi.fn(),
    listMcpConnections: vi.fn(),
    listMcpInvocations: vi.fn(),
    replaceMcpThreadBindings: vi.fn(),
    decideMcpInvocation: vi.fn(),
  };
});

import {
  decideMcpInvocation,
  getMcpConnectionCapabilities,
  getMcpThreadBindings,
  listMcpConnections,
  listMcpInvocations,
  replaceMcpThreadBindings,
} from '@/lib/api/mcp';

const mockedBindings = getMcpThreadBindings as unknown as ReturnType<typeof vi.fn>;
const mockedInvocations = listMcpInvocations as unknown as ReturnType<typeof vi.fn>;
const mockedReplace = replaceMcpThreadBindings as unknown as ReturnType<typeof vi.fn>;
const mockedDecide = decideMcpInvocation as unknown as ReturnType<typeof vi.fn>;
const mockedConnections = listMcpConnections as unknown as ReturnType<typeof vi.fn>;
const mockedCapabilities = getMcpConnectionCapabilities as unknown as ReturnType<typeof vi.fn>;

const binding: McpBindingApi = {
  connection_id: 3,
  connection_label: 'Team wiki',
  alias: 'wiki',
  enabled: false,
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

/** A connection the conversation is not bound to yet. */
const attachableConnection: McpConnectionApi = {
  id: 4,
  label: 'Issue tracker',
  template: null,
  transport: 'streamable-http',
  location: 'remote',
  url: 'https://issues.example.com/mcp',
  argv: [],
  auth_kind: 'none',
  status: 'active',
  error_kind: 'none',
  error_detail: null,
  account_label: null,
  connector_id: null,
  has_credential: false,
  secret_hint: null,
  expires_at: null,
  config_revision: 2,
  authorization_epoch: 1,
  capability_revision: 2,
  server_name: 'issues',
  server_version: '1.0.0',
  protocol_version: '2025-06-18',
  tool_count: 1,
  resource_count: 0,
  prompt_count: 0,
  support: {},
  created_at: null,
  updated_at: null,
};

beforeEach(() => {
  // Every test renders a panel that also reads the account's connections for
  // the attach list, even when it has nothing to attach.
  mockedConnections.mockResolvedValue({ connections: [attachableConnection], templates: [] });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('McpSessionPanel', () => {
  it('groups capabilities and asks the user to choose on a pending selection', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding, unavailable] });
    mockedInvocations.mockResolvedValue({ invocations: [] });

    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);

    expect(await screen.findByText('Choose what this session may use')).toBeInTheDocument();
    expect(screen.getByText('Tools')).toBeInTheDocument();
    expect(screen.getByText('Prompts')).toBeInTheDocument();
    expect(screen.getByText('Resource templates')).toBeInTheDocument();
    // A mapped binding defaults to confirm, so a pre-existing session keeps
    // working while its owner is asked once.
    const toolPolicy = screen.getByRole('radiogroup', { name: 'Policy for search' });
    expect(toolPolicy).toHaveTextContent('Confirm');
    // A failed connection reports why instead of offering a selection.
    expect(screen.getByText('the credential expired')).toBeInTheDocument();
  });

  it('saves every capability policy the user chose', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding] });
    mockedInvocations.mockResolvedValue({ invocations: [] });
    mockedReplace.mockResolvedValue({ bindings: [{ ...binding, selection_pending: false }] });

    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    await screen.findByText('Choose what this session may use');

    const group = screen.getByRole('radiogroup', { name: 'Policy for search' });
    fireEvent.click(within(group).getByRole('radio', { name: 'Allow' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save selection' }));

    await waitFor(() =>
      expect(mockedReplace).toHaveBeenCalledWith('thread-1', [
        {
          connection_id: 3,
          alias: 'wiki',
          enabled: true,
          tools: { search: 'allow' },
          resources: { 'page://{slug}': 'confirm' },
          prompts: { summarize: 'confirm' },
        },
      ]),
    );
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

  it('attaches an unbound connection with its capabilities denied by default', async () => {
    mockedBindings.mockResolvedValue({ bindings: [binding] });
    mockedInvocations.mockResolvedValue({ invocations: [] });
    mockedCapabilities.mockResolvedValue([
      {
        kind: 'tool',
        name: 'create_issue',
        description: 'Open an issue',
        input_schema: null,
        annotations: {},
      },
    ]);
    mockedReplace.mockResolvedValue({ bindings: [binding] });

    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'Attach Issue tracker' }));

    await waitFor(() => expect(mockedCapabilities).toHaveBeenCalledWith(4));
    // Nothing is allowed just because the server was attached; the control
    // starts at deny and the user has to say otherwise.
    expect(
      await screen.findByRole('radiogroup', { name: 'Policy for create_issue' }),
    ).toHaveTextContent('Deny');

    fireEvent.click(screen.getByRole('button', { name: 'Save selection' }));

    await waitFor(() =>
      expect(mockedReplace).toHaveBeenCalledWith('thread-1', [
        {
          connection_id: 3,
          alias: 'wiki',
          enabled: true,
          tools: { search: 'confirm' },
          resources: { 'page://{slug}': 'confirm' },
          prompts: { summarize: 'confirm' },
        },
        {
          connection_id: 4,
          alias: 'mcp4',
          enabled: false,
          tools: { create_issue: 'deny' },
          resources: {},
          prompts: {},
        },
      ]),
    );
  });

  it('waits to be opened before reading anything', () => {
    render(<McpSessionPanel jobId="job-1" threadId="thread-1" active={false} />);

    expect(screen.getByText('Open this panel to load tools.')).toBeInTheDocument();
    expect(mockedBindings).not.toHaveBeenCalled();
    expect(mockedInvocations).not.toHaveBeenCalled();
  });
});
