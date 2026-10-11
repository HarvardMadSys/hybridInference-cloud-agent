// Tests for the MCP connection API client.
//
// The contract has three shapes worth pinning: the import preview and its save
// are two different requests about the same paste, a thread id is not safe to
// interpolate unencoded, and a decision is a POST with a boolean rather than a
// status string.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  authorizeMcpConnection,
  createMcpConnection,
  decideMcpInvocation,
  disconnectMcpConnection,
  getMcpThreadBindings,
  listMcpConnections,
  listMcpInvocations,
  previewMcpImport,
  replaceMcpThreadBindings,
  saveMcpImport,
  testMcpConnection,
  validateMcpAuthorizationUrl,
} from '../mcp';
import * as client from '../client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCP browser authorization URL validation', () => {
  it.each([
    'javascript:alert(document.domain)',
    'JaVaScRiPt:alert(document.domain)',
    'data:text/html,<script>alert(1)</script>',
    'file:///tmp/authorize',
    'http://localhost:8080/authorize',
    '/authorize',
    '//login.example.com/authorize',
    'https:login.example.com/authorize',
    'https:///authorize',
    'https://login.example.com:invalid/authorize',
    'https://login.example.com:65536/authorize',
    'https://[invalid]/authorize',
    'https://user:password@login.example.com/authorize',
    'https://@login.example.com/authorize',
    'https://login.example.com/authorize#fragment',
    'https://login.example.com/authorize#',
    'https://login.example.com\\authorize',
    'https://login.\nexample.com/authorize',
    ' https://login.example.com/authorize',
    'https://login.example.com/authorize\u0000',
  ])('rejects an unsafe or ambiguous redirect: %s', (value) => {
    expect(() => validateMcpAuthorizationUrl(value)).toThrow('unsafe sign-in address');
  });

  it.each([
    'https://auth.example.com/authorize',
    'https://login.other.example:8443/oauth/authorize?audience=mcp%3Aread&state=state',
    'HTTPS://Login.other.example/authorize?prompt=consent',
    'https://[::1]:8443/authorize',
  ])('preserves a valid absolute HTTPS destination: %s', (value) => {
    expect(validateMcpAuthorizationUrl(value)).toBe(value);
  });
});

describe('mcp api', () => {
  let fetchWithAuth: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchWithAuth = vi.spyOn(client, 'fetchWithAuth');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('lists connections and their templates from one read', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ connections: [], templates: [] }));

    await expect(listMcpConnections()).resolves.toEqual({ connections: [], templates: [] });
    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/mcp/connections');
  });

  it('previews an import without saving it', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ drafts: [], rejected: [], dialect: 'json' }));

    await previewMcpImport('{"mcpServers":{}}', 'json');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/connections/import',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: '{"mcpServers":{}}',
          format_hint: 'json',
          save: false,
        }),
      },
    );
  });

  it('saves the same paste with the run locations the user chose', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ drafts: [], saved: [], rejected: [], dialect: 'json' }),
    );

    await saveMcpImport('paste', { files: 'managed-worker' }, { TOKEN: 'secret' });

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/connections/import',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: 'paste',
          format_hint: undefined,
          save: true,
          locations: { files: 'managed-worker' },
          env_secrets: { TOKEN: 'secret' },
        }),
      },
    );
  });

  it('adds one connection by hand', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 4, label: 'Wiki' }));

    await createMcpConnection({
      label: 'Wiki',
      transport: 'stdio',
      location: 'managed-worker',
      launch: { command: 'npx', args: ['-y', 'server'], env: {}, cwd: null },
      auth_kind: 'none',
    });

    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/mcp/connections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        label: 'Wiki',
        transport: 'stdio',
        location: 'managed-worker',
        launch: { command: 'npx', args: ['-y', 'server'], env: {}, cwd: null },
        auth_kind: 'none',
      }),
    });
  });

  it('tests a connection through its own diagnostic route', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ ok: true }));

    await testMcpConnection(7);

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/connections/7/test',
      { method: 'POST' },
    );
  });

  it('disconnects without trying to read a 204 body', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(disconnectMcpConnection(7)).resolves.toBeUndefined();
    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/mcp/connections/7', {
      method: 'DELETE',
    });
  });

  it('sends a client id only when the user supplied one', async () => {
    // A fresh response per call: a Response body can only be read once.
    fetchWithAuth.mockImplementation(async () =>
      jsonResponse({ authorization_url: null, needs_client_id: true, message: 'need one' }),
    );

    await expect(authorizeMcpConnection(7)).resolves.toEqual({
      authorization_url: null,
      needs_client_id: true,
      message: 'need one',
    });
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/mcp/connections/7/authorize',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      },
    );

    await authorizeMcpConnection(7, 'client-abc');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/mcp/connections/7/authorize',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: 'client-abc' }),
      },
    );
  });

  it('encodes a thread id in the bindings path', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ bindings: [] }));

    await getMcpThreadBindings('thread/one');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/threads/thread%2Fone/bindings',
    );
  });

  it('replaces a conversation selection wholesale', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ bindings: [] }));

    await replaceMcpThreadBindings('t1', [
      {
        connection_id: 3,
        alias: 'wiki',
        enabled: true,
        tools: { search: 'allow' },
        resources: {},
        prompts: {},
      },
    ]);

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/threads/t1/bindings',
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bindings: [
            {
              connection_id: 3,
              alias: 'wiki',
              enabled: true,
              tools: { search: 'allow' },
              resources: {},
              prompts: {},
            },
          ],
        }),
      },
    );
  });

  it('asks only for pending calls when the panel is a queue', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ invocations: [] }));

    await listMcpInvocations({ jobId: 'job/1', pendingOnly: true });

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/invocations?job_id=job%2F1&pending_only=true',
    );
  });

  it('reads owner-wide pending pages using the returned keyset cursor', async () => {
    const response = { invocations: [], has_more: true, next_before_id: 51 };
    fetchWithAuth.mockResolvedValue(jsonResponse(response));
    await expect(
      listMcpInvocations({ pendingOnly: true, limit: 50, beforeId: 100 }),
    ).resolves.toEqual(response);
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/invocations?pending_only=true&limit=50&before_id=100',
    );
  });

  it('paginates scoped request history with an encoded conversation ID', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ invocations: [], has_more: false, next_before_id: null }),
    );
    await listMcpInvocations({ threadId: 'conversation/1', limit: 100, beforeId: 9 });
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/invocations?thread_id=conversation%2F1&limit=100&before_id=9',
    );
  });

  it('decides a held call with a boolean', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 9, status: 'denied' }));

    await decideMcpInvocation(9, false);

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/mcp/invocations/9/decision',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: false }),
      },
    );
  });
});
