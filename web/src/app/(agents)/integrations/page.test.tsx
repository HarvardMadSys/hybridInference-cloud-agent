// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import IntegrationsPage from './page';
import type { McpConnectionApi } from '@/lib/api/mcp';

const navigation = vi.hoisted(() => ({ search: '' }));
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(navigation.search),
}));
import { listSkills } from '@/lib/api/skills';
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn() }));

vi.mock('@/lib/api/agents', () => ({
  getAgentIntegrations: vi.fn(),
  disconnectAgentIntegration: vi.fn(),
}));
vi.mock('@/lib/api/mcp', () => ({
  listMcpConnections: vi.fn(),
  testMcpConnection: vi.fn(),
  disconnectMcpConnection: vi.fn(),
  authorizeMcpConnection: vi.fn(),
}));

import { getAgentIntegrations } from '@/lib/api/agents';
import { listMcpConnections, testMcpConnection } from '@/lib/api/mcp';

const connection: McpConnectionApi = {
  id: 3,
  label: 'Team wiki',
  template: null,
  transport: 'streamable-http',
  location: 'remote',
  url: 'https://wiki.example.com/mcp',
  argv: [],
  auth_kind: 'oauth',
  status: 'draft',
  error_kind: 'none',
  error_detail: null,
  account_label: null,
  connector_id: null,
  has_credential: true,
  secret_hint: null,
  expires_at: null,
  config_revision: 1,
  authorization_epoch: 1,
  capability_revision: 1,
  server_name: null,
  server_version: null,
  protocol_version: null,
  tool_count: 0,
  resource_count: 0,
  prompt_count: 0,
  support: {},
  created_at: null,
  updated_at: null,
};

beforeEach(() => {
  vi.mocked(listSkills).mockResolvedValue([]);
  navigation.search = '';
  vi.mocked(getAgentIntegrations).mockResolvedValue({ providers: [] });
  vi.mocked(listMcpConnections).mockResolvedValue({ connections: [connection], templates: [] });
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('MCP authorization return', () => {
  it('discovers tools automatically after a verified authorization return', async () => {
    navigation.search = 'mcp_authorized=1&mcp_connection=3';
    vi.mocked(testMcpConnection).mockResolvedValue({
      ok: true,
      error_kind: 'none',
      message: '',
      protocol_version: '2025-11-25',
      server_name: 'wiki',
      server_version: '1',
      tool_count: 2,
      resource_count: 0,
      prompt_count: 0,
      support: {},
      capabilities: [],
    });
    render(<IntegrationsPage />);
    expect(await screen.findByText('Team wiki connected. 2 tools.')).toBeInTheDocument();
    expect(vi.mocked(testMcpConnection)).toHaveBeenCalledWith(3);
    await waitFor(() => expect(vi.mocked(listMcpConnections)).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Team wiki authorized/)).not.toBeInTheDocument();
  });

  it('shows why authorization did not complete and keeps retry available', async () => {
    navigation.search = 'mcp_authorized=0&mcp_connection=3&mcp_message=access_denied';
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [{ ...connection, has_credential: false }],
      templates: [],
    });
    render(<IntegrationsPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('access_denied');
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
    expect(screen.queryByText(/Team wiki authorized/)).not.toBeInTheDocument();
  });

  it('verifies the stored credential before announcing authorization success', async () => {
    navigation.search = 'mcp_authorized=1&mcp_connection=3';
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [{ ...connection, has_credential: false }],
      templates: [],
    });
    render(<IntegrationsPage />);
    await screen.findByText('Team wiki');
    expect(screen.queryByText(/Team wiki authorized/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
  });
});
