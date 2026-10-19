import { beforeEach, describe, expect, it, vi } from 'vitest';
import { expectRequestsMatchContract } from './__tests__/contract';
import {
  drainAgentRunnerHost,
  forgetAgentRunnerHost,
  getAgentRunnerHostCapacityHistory,
  getAgentRunnerHosts,
  listAdminAuditLog,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentHostSettings,
  setAgentRunnerHostSettings,
  setAgentRunnerHostSlots,
  setAgentRuntimeVersion,
  undrainAgentRunnerHost,
} from './admin';
import { fetchWithAuth } from './client';

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  fetchWithAuth: vi.fn(),
}));

const pool = { hosts: [], active_host: null, runtime_versions: {}, observed_at: null };

function lastCall() {
  const [, path, init] = vi.mocked(fetchWithAuth).mock.calls.at(-1)!;
  return { path, init: init ?? {}, body: init?.body ? JSON.parse(String(init.body)) : undefined };
}

describe('admin runner host API client', () => {
  beforeEach(() => {
    vi.mocked(fetchWithAuth).mockReset();
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(JSON.stringify(pool)));
  });

  it('sends only requests the server accepts', async () => {
    await getAgentRunnerHosts();
    await getAgentRunnerHostCapacityHistory('host/a', '24h');
    await setActiveAgentRunnerHost('host/a');
    await setActiveAgentRunnerHost(null);
    await setAgentRuntimeVersion('claude-code', '2.1.300');
    await setAgentRuntimeVersion('claude-code', null);
    await forgetAgentRunnerHost('host/a');
    await drainAgentRunnerHost('host/a');
    await undrainAgentRunnerHost('host/a');
    await setAgentRunnerHostSlots('host/a', 8);
    await setAgentRunnerHostSlots('host/a', null);
    await setAgentHostSettings({ idle_timeout_seconds: 600, terminal_limit: null });
    await setAgentRunnerHostSettings('host/a', { idle_timeout_seconds: null });
    await removeAgentRunnerHost('host/a');
    await removeAgentRunnerHost('host/a', true, true);
    await listAdminAuditLog();
    await listAdminAuditLog({ category: 'servers', before: 120 });
    expectRequestsMatchContract();
  });

  it('names a host by its escaped enrollment name in every action', async () => {
    await drainAgentRunnerHost('host/a');
    expect(lastCall()).toMatchObject({
      path: '/admin/agent/runner-hosts/host%2Fa/drain',
      init: { method: 'POST' },
    });
    await undrainAgentRunnerHost('host/a');
    expect(lastCall().path).toBe('/admin/agent/runner-hosts/host%2Fa/undrain');
    await forgetAgentRunnerHost('host/a');
    expect(lastCall()).toMatchObject({
      path: '/admin/agent/runner-hosts/host%2Fa',
      init: { method: 'DELETE' },
    });
    await getAgentRunnerHostCapacityHistory('host/a', '6h');
    expect(lastCall().path).toBe('/admin/agent/runner-hosts/host%2Fa/capacity-history?range=6h');
  });

  it('removes gently unless asked to end running work or leave sandboxes behind', async () => {
    await removeAgentRunnerHost('host-a');
    expect(lastCall().path).toBe('/admin/agent/runner-hosts/host-a/remove');
    await removeAgentRunnerHost('host-a', false, true);
    expect(lastCall().path).toBe('/admin/agent/runner-hosts/host-a/remove?allow_unmoved=true');
    await removeAgentRunnerHost('host-a', true);
    expect(lastCall().path).toBe('/admin/agent/runner-hosts/host-a/remove?force=true');
  });

  it('sends null to return a setting to the default rather than leaving it out', async () => {
    await setAgentRunnerHostSlots('host-a', null);
    expect(lastCall()).toMatchObject({ init: { method: 'PUT' }, body: { slots: null } });
    await setAgentRuntimeVersion('codex', null);
    expect(lastCall()).toMatchObject({
      path: '/admin/agent/runtime-versions',
      body: { runtime: 'codex', version: null },
    });
    await setActiveAgentRunnerHost(null);
    expect(lastCall()).toMatchObject({
      path: '/admin/agent/runner-hosts/active',
      body: { host: null },
    });
    await setAgentHostSettings({ idle_timeout_seconds: null });
    expect(lastCall()).toMatchObject({
      path: '/admin/agent/host-settings',
      body: { settings: { idle_timeout_seconds: null } },
    });
  });

  it('pages the audit log by category and the last id read', async () => {
    vi.mocked(fetchWithAuth).mockImplementation(
      async () => new Response(JSON.stringify({ entries: [], next_before: null })),
    );
    await listAdminAuditLog();
    expect(lastCall().path).toBe('/admin/agent/audit-log');
    await listAdminAuditLog({ category: 'database', before: 41 });
    expect(lastCall().path).toBe('/admin/agent/audit-log?category=database&before=41');
  });

  it('surfaces a refused removal with its code', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValue(
      new Response(
        JSON.stringify({ error: { type: 'no_move_target', message: 'Nowhere to move.' } }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    await expect(removeAgentRunnerHost('host-a')).rejects.toMatchObject({
      code: 'no_move_target',
      statusCode: 409,
    });
  });
});
