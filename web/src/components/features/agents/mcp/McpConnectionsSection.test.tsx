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
    enableMcpOAuth: vi.fn(),
    createMcpConnection: vi.fn(),
    previewMcpImport: vi.fn(),
    saveMcpImport: vi.fn(),
  };
});

import {
  authorizeMcpConnection,
  disconnectMcpConnection,
  listMcpConnections,
  testMcpConnection,
  enableMcpOAuth,
  previewMcpImport,
  saveMcpImport,
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
    expect(screen.getByText('Sign-in required')).toBeInTheDocument();
    expect(screen.getByText('https://wiki.example.com/mcp', { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByText('the server rejected the credential')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Server details'));
    expect(screen.getByText('wiki 2.1.0')).toBeVisible();
    expect(screen.getByText('2025-06-18')).toBeVisible();
  });

  it('refreshes a connected server and reports discovered tools', async () => {
    mockedList.mockResolvedValue({
      ...api,
      connections: [{ ...connection, status: 'active', error_kind: 'none', has_credential: true }],
    });
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
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh tools' }));

    await waitFor(() => expect(mockedTest).toHaveBeenCalledWith(3));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Team wiki connected. 6 tools · 1 resource · 2 prompts.',
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
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }));

    expect(
      await screen.findByText('this server does not register clients for us'),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('OAuth client ID'), {
      target: { value: 'client-abc' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));

    await waitFor(() => expect(mockedAuthorize).toHaveBeenLastCalledWith(3, 'client-abc'));
    expect(await screen.findByRole('status')).toHaveTextContent('authorized');
  });

  it('disconnects a connection after confirming', async () => {
    mockedDisconnect.mockResolvedValue(undefined);

    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByText('Server details'));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));

    await waitFor(() => expect(mockedDisconnect).toHaveBeenCalledWith(3));
    expect(await screen.findByRole('status')).toHaveTextContent('Team wiki disconnected.');
  });

  it('leaves the connection alone when the confirmation is refused', async () => {
    vi.stubGlobal(
      'confirm',
      vi.fn(() => false),
    );

    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByText('Server details'));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));

    expect(mockedDisconnect).not.toHaveBeenCalled();
  });

  it('offers the add dialog when there is nothing connected', async () => {
    mockedList.mockResolvedValue({ connections: [], templates: [] });

    render(<McpConnectionsSection />);

    expect(
      await screen.findByText(
        'Connect a popular service or paste an MCP JSON configuration to get started.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add server' })).toBeInTheDocument();
  });

  it('discovers tools automatically after returning from sign-in', async () => {
    mockedList.mockResolvedValue({
      ...api,
      connections: [{ ...connection, has_credential: true }],
    });
    mockedTest.mockResolvedValue({ ok: true, tool_count: 4, resource_count: 0, prompt_count: 0 });
    render(
      <McpConnectionsSection
        authorizationResult={{ ok: true, connectionId: '3', message: null }}
      />,
    );
    expect(await screen.findByText('Team wiki connected. 4 tools.')).toBeInTheDocument();
    expect(mockedTest).toHaveBeenCalledTimes(1);
  });

  it('does not show a failed handshake as a success notice', async () => {
    mockedList.mockResolvedValue({
      ...api,
      connections: [{ ...connection, auth_kind: 'none', error_kind: 'unreachable' }],
    });
    mockedTest.mockResolvedValue({ ok: false, message: 'Server is unavailable' });
    render(<McpConnectionsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry connection' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Server is unavailable');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('opts a pasted URL into OAuth only when the user chooses Sign in', async () => {
    const pasted = { ...connection, auth_kind: 'none' as const };
    mockedList.mockResolvedValue({ ...api, connections: [pasted] });
    vi.mocked(enableMcpOAuth).mockResolvedValue({
      ...pasted,
      auth_kind: 'oauth',
      config_revision: 2,
    });
    mockedAuthorize.mockResolvedValue({ needs_client_id: true, message: 'Enter client ID' });
    render(<McpConnectionsSection />);
    await screen.findByText('Team wiki');
    expect(enableMcpOAuth).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(enableMcpOAuth).toHaveBeenCalledWith(pasted));
    expect(mockedAuthorize).toHaveBeenCalledWith(3, undefined);
  });

  it('connects imported servers automatically without another Test click', async () => {
    const saved = { ...connection, auth_kind: 'none' as const, status: 'draft' as const };
    mockedList.mockResolvedValueOnce({ connections: [], templates: [] }).mockResolvedValue({
      connections: [{ ...saved, status: 'active', error_kind: 'none', error_detail: null }],
      templates: [],
    });
    vi.mocked(previewMcpImport).mockResolvedValue({
      dialect: 'json',
      rejected: [],
      drafts: [
        {
          label: 'Team wiki',
          transport: 'streamable-http',
          location: 'remote',
          url: connection.url,
          argv: [],
          env_names: [],
          headers: [],
          secrets: [],
          paths: [],
          package: null,
          warnings: [],
        },
      ],
    });
    vi.mocked(saveMcpImport).mockResolvedValue({
      dialect: 'json',
      rejected: [],
      drafts: [],
      saved: [saved],
    });
    mockedTest.mockResolvedValue({ ok: true, tool_count: 4, resource_count: 0, prompt_count: 0 });
    render(<McpConnectionsSection />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Add with JSON' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add with JSON' }));
    fireEvent.change(screen.getByLabelText('MCP configuration'), {
      target: { value: '{"mcpServers":{"wiki":{"url":"https://wiki.example.com/mcp"}}}' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Add 1 server' }));
    expect(await screen.findByText('Team wiki connected. 4 tools.')).toBeInTheDocument();
    expect(mockedTest).toHaveBeenCalledWith(3);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
