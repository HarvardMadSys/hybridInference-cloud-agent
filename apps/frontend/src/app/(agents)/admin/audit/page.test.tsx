// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// Setup notices have their own tests; this page uses a configured deployment.
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminAuditLogEntry, AdminAuditLogResponse } from '@/lib/api/admin';
import type { AgentAccessApi } from '@/lib/api/session';

const auth = vi.hoisted(() => ({ id: 'u-me', access: undefined as AgentAccessApi | undefined }));
const address = vi.hoisted(() => ({ query: '' }));
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(address.query),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, role: 'free', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { ...actual };
});
vi.mock('@/lib/api/admin', () => ({
  listAdminAuditLog: vi.fn(),
  getAgentRunnerHosts: vi.fn(),
}));

import { getAgentRunnerHosts, listAdminAuditLog } from '@/lib/api/admin';

import AdminAuditLogPage from './page';

const list = vi.mocked(listAdminAuditLog);
const pool = vi.mocked(getAgentRunnerHosts);

function entry(id: number, over: Partial<AdminAuditLogEntry> = {}): AdminAuditLogEntry {
  return {
    id,
    action: 'agent.runner_host.drain',
    category: 'servers',
    kind: 'change',
    actor_id: 'admin:u-me',
    actor_type: 'admin',
    actor_account_id: 'u-me',
    actor_label: 'me',
    actor_deleted: false,
    admin_ip: '203.0.113.9',
    target_user_id: null,
    subject_label: null,
    subject_deleted: false,
    target_type: 'host',
    target_id: 'rtx6000b',
    target_label: null,
    details: { host: 'rtx6000b' },
    success: true,
    outcome: 'applied',
    result: null,
    result_at: null,
    reason: null,
    created_at: new Date(Date.now() - 120_000).toISOString(),
    ...over,
  };
}

function page(
  entries: AdminAuditLogEntry[],
  next: number | null = null,
  failures = 0,
): AdminAuditLogResponse {
  return { entries, next_before: next, write_failures: { count: failures, latest_at: null } };
}

const FIRST = { category: null, actors: 'people', user: null, target: null, before: null };

beforeEach(() => {
  address.query = '';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  list.mockReset().mockResolvedValue(page([]));
  pool.mockReset().mockResolvedValue({
    hosts: [],
    active_host: null,
    runtime_versions: {},
    observed_at: null,
    host_setting_definitions: [
      {
        key: 'agent_timeout_s',
        env: 'AGENT_TIMEOUT_S',
        label: 'Turn time limit',
        description: '',
        unit: 'seconds',
        default: 3600,
        minimum: 60,
        maximum: 86400,
        scope: 'runner',
      },
    ],
  });
});

afterEach(() => {
  cleanup();
});

describe('admin Audit log', () => {
  it('requires an explicit admin permission before reading anything', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminAuditLogPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });

  it('shows each change as who, what and when', async () => {
    list.mockResolvedValue(
      page([
        entry(3, {
          action: 'agent.host_settings.set',
          category: 'settings',
          details: { changes: { agent_timeout_s: 7200 }, old_values: { agent_timeout_s: 3600 } },
        }),
        entry(2, {
          actor_id: 'admin:01KQGM0E6VEG598F',
          actor_account_id: '01KQGM0E6VEG598F',
          actor_label: 'juncheng',
        }),
        entry(1, { action: 'agent.runner_host.forget', success: false, outcome: 'failed' }),
      ]),
    );
    render(<AdminAuditLogPage />);

    expect(
      await screen.findByText('Set Turn time limit to 2 h for all servers (was 1 h)'),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Audit log' })).toHaveAttribute('aria-current', 'page');
    const rows = screen.getAllByRole('listitem');
    expect(within(rows[0]).getByText('You')).toBeInTheDocument();
    expect(within(rows[0]).getByText('2 min ago')).toBeInTheDocument();
    expect(within(rows[1]).getByText('juncheng (01KQGM0E…)')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Paused new work on rtx6000b')).toBeInTheDocument();
    expect(within(rows[2]).getByText('Failed')).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(FIRST, expect.anything());
  });

  it('asks for one kind of change at a time', async () => {
    list.mockResolvedValueOnce(page([entry(2)]));
    list.mockResolvedValueOnce(
      page([entry(9, { action: 'agent.run.event_content.read', details: { job_id: 'ajob_7f' } })]),
    );
    render(<AdminAuditLogPage />);
    await screen.findByText('Paused new work on rtx6000b');

    fireEvent.click(screen.getByRole('button', { name: 'Data access' }));

    expect(await screen.findByText("Viewed a step's content in turn ajob_7f")).toBeInTheDocument();
    expect(screen.queryByText('Paused new work on rtx6000b')).not.toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith(
      { ...FIRST, category: 'sensitive_access' },
      expect.anything(),
    );
    expect(screen.getByRole('button', { name: 'Data access' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('pages back through older changes', async () => {
    list.mockResolvedValueOnce(page([entry(5)], 5));
    list.mockResolvedValueOnce(page([entry(4, { action: 'agent.runner_host.undrain' })]));
    render(<AdminAuditLogPage />);
    await screen.findByText('Paused new work on rtx6000b');

    fireEvent.click(screen.getByRole('button', { name: 'Show older' }));

    expect(await screen.findByText('Resumed new work on rtx6000b')).toBeInTheDocument();
    expect(screen.getByText('Paused new work on rtx6000b')).toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith({ ...FIRST, before: 5 }, expect.anything());
    expect(screen.queryByRole('button', { name: 'Show older' })).not.toBeInTheDocument();
  });

  it('opens the recorded action, address and details under a row', async () => {
    list.mockResolvedValue(
      page([entry(1, { target_user_id: 'user-7', subject_label: 'alice@example.com' })]),
    );
    render(<AdminAuditLogPage />);
    const row = await screen.findByRole('button', { name: /Paused new work on rtx6000b/ });
    expect(screen.queryByText('203.0.113.9')).not.toBeInTheDocument();

    fireEvent.click(row);

    expect(row).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('agent.runner_host.drain')).toBeInTheDocument();
    expect(screen.getByText('203.0.113.9')).toBeInTheDocument();
    expect(screen.getByText('user-7')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Show only this account' })).toHaveAttribute(
      'href',
      '/admin/audit?user=user-7',
    );
    expect(screen.getByRole('link', { name: 'Show only this' })).toHaveAttribute(
      'href',
      '/admin/audit?target_type=host&target_id=rtx6000b',
    );
    expect(screen.getByText(/"host": "rtx6000b"/)).toBeInTheDocument();
  });

  it('gives the exact time of a change in UTC, as every admin page does', async () => {
    list.mockResolvedValue(page([entry(1, { created_at: '2026-10-07T13:00:00Z' })]));
    render(<AdminAuditLogPage />);
    const row = await screen.findByRole('button', { name: /Paused new work on rtx6000b/ });
    expect(within(row).getByText('Oct 7')).toHaveAttribute(
      'title',
      expect.stringMatching(/^Oct 7, 2026, 1:00:00\sPM UTC$/),
    );

    fireEvent.click(row);

    expect(screen.getByText(/^Oct 7, 2026, 1:00:00\sPM UTC$/)).toBeInTheDocument();
  });

  it('says when nothing is recorded, and retries a failed read', async () => {
    list.mockRejectedValueOnce(new Error('Service unavailable'));
    render(<AdminAuditLogPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('Nothing recorded here yet.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('shows a replayed Stop as a repeat, not as measurements turned off', async () => {
    list.mockResolvedValue(
      page([
        entry(1, {
          action: 'agent.profiling.stop',
          category: 'measurements',
          details: { outcome: 'replayed', run_id: 'admin-1', revision: 2 },
        }),
      ]),
    );
    render(<AdminAuditLogPage />);

    const row = await screen.findByRole('button', {
      name: /Asked to turn measurements off again; nothing changed/,
    });
    expect(screen.queryByText('Turned measurements off')).not.toBeInTheDocument();
    // A repeat is not a failure; the sentence says what happened.
    expect(within(row).queryByText('Refused')).not.toBeInTheDocument();
    expect(within(row).queryByText('Failed')).not.toBeInTheDocument();
  });

  it('marks a refused Stop as refused, even on a row recorded as successful', async () => {
    list.mockResolvedValue(
      page([
        entry(1, {
          action: 'agent.profiling.stop',
          category: 'measurements',
          success: true,
          details: { outcome: 'refused', reason: 'run_already_finished', run_id: 'admin-1' },
        }),
      ]),
    );
    render(<AdminAuditLogPage />);

    const row = await screen.findByRole('button', {
      name: /Tried to turn measurements off \(refused: run_already_finished\)/,
    });
    expect(within(row).getByText('Refused')).toBeInTheDocument();
    expect(screen.queryByText('Turned measurements off')).not.toBeInTheDocument();
  });

  it('never pages a new filter with the cursor of the old one', async () => {
    list.mockResolvedValueOnce(page([entry(5)], 5));
    list.mockRejectedValueOnce(new Error('Service unavailable'));
    render(<AdminAuditLogPage />);
    expect(await screen.findByRole('button', { name: 'Show older' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Data access' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    // All's cursor would skip the newer data access rows; it must not be offered.
    expect(screen.queryByRole('button', { name: 'Show older' })).not.toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('shows people unless asked to include automation', async () => {
    list.mockResolvedValue(page([entry(1)]));
    render(<AdminAuditLogPage />);
    await screen.findByText('Paused new work on rtx6000b');

    fireEvent.click(screen.getByRole('checkbox', { name: /Include automation/ }));

    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith({ ...FIRST, actors: 'all' }, expect.anything()),
    );
  });

  it('marks access that belongs to no account', async () => {
    list.mockResolvedValue(
      page([
        entry(1, { actor_id: 'admin-token', actor_type: 'break_glass', actor_account_id: null }),
      ]),
    );
    render(<AdminAuditLogPage />);
    const actor = await screen.findByText('Admin token');
    expect(actor).toHaveClass('text-red-600');
  });

  it("narrows to one account's whole history from the address", async () => {
    address.query = 'user=acct-7';
    list.mockResolvedValue(
      page([
        entry(1, {
          action: 'agent.local_account.approve',
          category: 'accounts',
          target_type: 'account',
          target_id: 'acct-7',
          target_user_id: 'acct-7',
          subject_label: 'alice@example.com',
          details: {},
        }),
      ]),
    );
    render(<AdminAuditLogPage />);

    expect(await screen.findByText('Approved alice@example.com (acct-7)')).toBeInTheDocument();
    expect(screen.getByText('alice@example.com (acct-7)')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Show everything' })).toHaveAttribute(
      'href',
      '/admin/audit',
    );
    // An account's history includes what it did itself.
    expect(list).toHaveBeenCalledWith(
      { ...FIRST, actors: 'all', user: 'acct-7' },
      expect.anything(),
    );
  });

  it('narrows to one object from the address', async () => {
    address.query = 'target_type=sandbox&target_id=asbx_1';
    render(<AdminAuditLogPage />);
    expect(await screen.findByText('sandbox asbx_1')).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(
      { ...FIRST, target: { type: 'sandbox', id: 'asbx_1' } },
      expect.anything(),
    );
  });

  it("shows a request's final result beside it", async () => {
    list.mockResolvedValue(
      page([
        entry(2, {
          action: 'agent.sandbox.delete.requested',
          outcome: 'requested',
          details: { sandbox_id: 'asbx_1' },
          result: 'failed',
          result_at: new Date().toISOString(),
        }),
        entry(1, { action: 'agent.compute.server.add', outcome: 'requested', details: {} }),
      ]),
    );
    render(<AdminAuditLogPage />);
    expect(await screen.findByText(/Failed 2 min later/)).toBeInTheDocument();
    expect(screen.getByText(/Still in progress/)).toBeInTheDocument();
  });

  it('says when actions could not be recorded', async () => {
    list.mockResolvedValue(page([entry(1)], null, 2));
    render(<AdminAuditLogPage />);
    expect(
      await screen.findByText(/2 actions in the last 7 days could not be recorded/),
    ).toBeInTheDocument();
  });
});
