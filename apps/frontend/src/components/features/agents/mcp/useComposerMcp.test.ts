// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { McpConnectionApi } from '@/lib/api/mcp';
import { getMcpConnectionCapabilities, listMcpConnections } from '@/lib/api/mcp';
import { useComposerMcp } from './useComposerMcp';

vi.mock('@/lib/api/mcp', async (original) => ({
  ...(await original<typeof import('@/lib/api/mcp')>()),
  listMcpConnections: vi.fn(),
  getMcpConnectionCapabilities: vi.fn(),
}));

const t = ((key: string) => key) as Parameters<typeof useComposerMcp>[0];

function connection(fields: Partial<McpConnectionApi>): McpConnectionApi {
  return {
    id: 1,
    label: 'Docs',
    template: null,
    transport: 'streamable-http',
    location: 'remote',
    url: 'https://docs.example.com/mcp',
    argv: [],
    auth_kind: 'none',
    status: 'draft',
    error_kind: 'none',
    error_detail: null,
    account_label: null,
    has_credential: false,
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
    ...fields,
  } as McpConnectionApi;
}

const program = connection({
  id: 2,
  label: 'Playwright',
  transport: 'stdio',
  location: 'sandbox',
  url: null,
  argv: ['npx', '-y', '@playwright/mcp@0.0.83'],
});

function listing(connections: McpConnectionApi[]) {
  return { connections, templates: [] } as unknown as Awaited<
    ReturnType<typeof listMcpConnections>
  >;
}

describe('useComposerMcp', () => {
  it('lets a chat turn on a program nobody could test, and still holds back an untested service', async () => {
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [connection({ id: 1, label: 'Service' }), program],
      templates: [],
    } as Awaited<ReturnType<typeof listMcpConnections>>);

    const { result } = renderHook(() => useComposerMcp(t));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const byLabel = Object.fromEntries(
      result.current.choices.map((choice) => [choice.connection.label, choice]),
    );
    expect(byLabel.Service).toMatchObject({ blocked: true, error: 'attach.mcpDraft' });
    expect(byLabel.Playwright.blocked).toBe(false);
    expect(byLabel.Playwright.error).toBeUndefined();
  });

  it('turns on a program that has not listed its tools yet, every tool asking first', async () => {
    vi.mocked(listMcpConnections).mockResolvedValue(listing([program]));
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);

    const { result } = renderHook(() => useComposerMcp(t));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(() => result.current.toggle(program, true));

    expect(result.current.choices[0]).toMatchObject({ selected: true, error: undefined });
    expect(result.current.bindings).toEqual([
      { connection_id: 2, tools: {}, enabled: true, use_defaults: true },
    ]);
  });

  it('still asks for a test before turning on a service that lists no tools', async () => {
    const service = connection({ id: 1, label: 'Service', status: 'active' });
    vi.mocked(listMcpConnections).mockResolvedValue(listing([service]));
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);

    const { result } = renderHook(() => useComposerMcp(t));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(() => result.current.toggle(service, true));

    expect(result.current.choices[0]).toMatchObject({
      selected: false,
      error: 'attach.mcpNoTools',
    });
    expect(result.current.bindings).toEqual([]);
  });
});
