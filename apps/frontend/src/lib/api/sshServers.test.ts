import { beforeEach, describe, expect, it, vi } from 'vitest';
import { expectRequestsMatchContract } from './__tests__/contract';
import { fetchWithAuth } from './client';
import {
  createSshServer,
  deleteSshServer,
  getSshThreadBindings,
  listSshServers,
  replaceSshThreadBindings,
  rotateSshServerKey,
  scanSshServer,
  updateSshServer,
  verifySshServer,
  type SshServerApi,
} from './sshServers';

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  fetchWithAuth: vi.fn(),
}));

const server: SshServerApi = {
  id: 'assh/1',
  name: 'build box',
  description: '',
  address: 'build.example',
  port: 22,
  username: 'ci',
  status: 'pending',
  pinned_address: null,
  host_key: null,
  scan: null,
  public_key: 'ssh-ed25519 AAAA',
  install_command: 'echo ...',
  pending_public_key: null,
  pending_install_command: null,
  last_check_at: null,
  last_check_error: null,
  last_check_message: null,
  revision: 3,
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
};

function answer(body: unknown) {
  vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(JSON.stringify(body)));
}

function lastCall() {
  const [, path, init] = vi.mocked(fetchWithAuth).mock.calls.at(-1)!;
  return { path, init: init ?? {}, body: init?.body ? JSON.parse(String(init.body)) : undefined };
}

describe('SSH server API client', () => {
  beforeEach(() => {
    vi.mocked(fetchWithAuth).mockReset();
  });

  it('sends only requests the server accepts', async () => {
    answer(server);
    await createSshServer({
      name: 'build box',
      address: 'build.example',
      port: 22,
      username: 'ci',
      description: '',
    });
    await updateSshServer(server, { port: 2222 });
    await scanSshServer(server);
    await verifySshServer(server, 'SHA256:abc');
    await rotateSshServerKey(server);
    answer({ configured: true, servers: [server] });
    await listSshServers();
    answer({ bindings: [] });
    await getSshThreadBindings('athr/1');
    await replaceSshThreadBindings('athr/1', ['assh/1']);
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(null, { status: 204 }));
    await deleteSshServer(server);
    expectRequestsMatchContract();
  });

  it('sends the revision the page read with every change to a server', async () => {
    answer(server);
    await updateSshServer(server, { name: 'renamed' });
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/ssh-servers/assh%2F1',
      init: { method: 'PATCH' },
      body: { revision: 3, name: 'renamed' },
    });
    await scanSshServer(server);
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/ssh-servers/assh%2F1/scan',
      body: { revision: 3 },
    });
    await rotateSshServerKey(server);
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/ssh-servers/assh%2F1/rotate-key',
      body: { revision: 3 },
    });
  });

  it('confirms exactly the host key fingerprint the owner saw', async () => {
    answer({ ...server, status: 'ready' });
    await verifySshServer(server, 'SHA256:abc');
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/ssh-servers/assh%2F1/verify',
      init: { method: 'POST' },
      body: { revision: 3, fingerprint: 'SHA256:abc' },
    });
  });

  it("replaces a chat's whole selection and unwraps the answer", async () => {
    const bindings = [{ server_id: 'assh/1', name: 'build box', status: 'ready', enabled: true }];
    answer({ bindings });
    await expect(replaceSshThreadBindings('athr/1', ['assh/1'])).resolves.toEqual(bindings);
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/ssh-servers/threads/athr%2F1/bindings',
      init: { method: 'PUT' },
      body: { server_ids: ['assh/1'] },
    });
    await expect(getSshThreadBindings('athr/1')).resolves.toEqual(bindings);
    expect(lastCall().init.cache).toBe('no-store');
  });

  it('surfaces a refused delete as an error', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValue(
      new Response(JSON.stringify({ error: { type: 'ssh_server_in_use', message: 'In use.' } }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await expect(deleteSshServer(server)).rejects.toMatchObject({
      code: 'ssh_server_in_use',
      statusCode: 409,
    });
  });
});
