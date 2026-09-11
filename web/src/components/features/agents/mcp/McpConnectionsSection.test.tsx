// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { McpConnectionsSection } from './McpConnectionsSection';
import type { McpConnectionApi, McpConnectionsApi } from '@/lib/api/mcp';

vi.mock('@/lib/api/mcp', async (importOriginal) => {
  // The dialog imports the same module for its create call; only the network
  // calls are replaced, so the types and helpers keep working.
  const actual = await importOriginal<typeof import('@/lib/api/mcp')>();
  return {
    ...actual,
    listMcpConnections: vi.fn(),
    testMcpConnection: vi.fn(),
    disconnectMcpConnection: vi.fn(),
    authorizeMcpConnection: vi.fn(),
  };
});

import {
  authorizeMcpConnection,
  disconnectMcpConnection,
  listMcpConnections,
  testMcpConnection,
} from '@/lib/api/mcp';

const mockedList = listMcpConnections as unknown as ReturnType<typeof vi.fn>;
const mockedTest = testMcpConnection as unknown as ReturnType<typeof vi.fn>;
const mockedDisconnect = disconnectMcpConnection as unknown as ReturnType<typeof vi.fn>;
const mockedAuthorize = authorizeMcpConnection as unknown as ReturnType<typeof vi.fn>;

const connection: McpConnectionApi = {
  id: 3,
  label: 'Team wiki',
  template: null,
  transport: 'streamable-http',
  location: 'remote',
  url: 'https://wiki.example.com/mcp',
  argv: [],
  auth_kind: 'oauth',
  status: 'error',
  error_kind: 'needs_authorization',
  error_detail: 'the server rejected the credential',
  account_label: 'wiki-bot',
  connector_id: null,
  has_credential: false,
  secret_hint: null,
  expires_at: null,
  config_revision: 1,
  authorization_epoch: 1,
  capability_revision: 1,
  server_name: 'wiki',
  server_version: '2.1.0',
  protocol_version: '2025-06-18',
  tool_count: 4,
  resource_count: 1,
  prompt_count: 2,
  support: {},
  created_at: null,
  updated_at: null,
};

const api: McpConnectionsApi = { connections: [connection], templates: [] };

beforeEach(() => {
  mockedList.mockResolvedValue(api);
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe('McpConnectionsSection', () => {
  it('renders what a connection is, where it runs and what it offers', async () => {
    render(<McpConnectionsSection />);

    expect(await screen.findByText('Team wiki')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
    expect(
      screen.getByText('Streamable HTTP · Remote server · https://wiki.example.com/mcp'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('wiki 2.1.0 · protocol 2025-06-18 · 4 tools · 1 resource · 2 prompts'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('needs_authorization: the server rejected the credential'),
    ).toBeInTheDocument();
    expect(screen.getByText('Not authorized yet.')).toBeInTheDocument();
  });

  it('tests a connection and reports the diagnosed server', async () => {
    mockedTest.mockResolvedValue({
      ok: true,
      error_kind: 'none',
      message: '',
      protocol_version: '2025-06-18',
      server_name: 'wiki',
      server_version: '2.2.0',
      tool_count: 6,
      resource_count: 1,
      prompt_count: 2,
      support: {},
      capabilities: [],
    });

    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Test' }));

    await waitFor(() => expect(mockedTest).toHaveBeenCalledWith(3));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Team wiki answered as wiki 2.2.0. 6 tools · 1 resource · 2 prompts',
    );
    // A test records its outcome on the connection, so the list is read again.
    expect(mockedList).toHaveBeenCalledTimes(2);
  });

  it('asks the server for a client id before authorizing', async () => {
    mockedAuthorize.mockResolvedValueOnce({
      authorization_url: null,
      needs_client_id: true,
      message: 'this server does not register clients for us',
    });
    mockedAuthorize.mockResolvedValueOnce({
      authorization_url: null,
      needs_client_id: false,
      message: 'authorized',
    });

    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Authorize' }));

    expect(
      await screen.findByText('this server does not register clients for us'),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('OAuth client id'), {
      target: { value: 'client-abc' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));

    await waitFor(() => expect(mockedAuthorize).toHaveBeenLastCalledWith(3, 'client-abc'));
    expect(await screen.findByRole('status')).toHaveTextContent('authorized');
  });

  it('disconnects a connection after confirming', async () => {
    mockedDisconnect.mockResolvedValue(undefined);

    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));

    await waitFor(() => expect(mockedDisconnect).toHaveBeenCalledWith(3));
    expect(await screen.findByRole('status')).toHaveTextContent('Team wiki disconnected.');
  });

  it('leaves the connection alone when the confirmation is refused', async () => {
    vi.stubGlobal(
      'confirm',
      vi.fn(() => false),
    );

    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));

    expect(mockedDisconnect).not.toHaveBeenCalled();
  });

  it('offers the add dialog when there is nothing connected', async () => {
    mockedList.mockResolvedValue({ connections: [], templates: [] });

    render(<McpConnectionsSection />);

    expect(
      await screen.findByText(
        'No MCP servers connected yet. Add one from a template, by pasting a configuration, or by filling in the address yourself.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add connection' })).toBeInTheDocument();
  });
});
