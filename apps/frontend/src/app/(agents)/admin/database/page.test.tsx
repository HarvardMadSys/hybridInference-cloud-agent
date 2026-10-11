// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// This page exercises pgAdmin; setup reminders use a configured deployment.
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

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ id: 'admin-a', access: undefined as AgentAccessApi | undefined }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, agent_access: auth.access } } }),
}));
vi.mock('@/lib/api/adminDatabase', () => ({
  getDatabaseTools: vi.fn(),
  setupPgAdmin: vi.fn(),
  getDatabaseTables: vi.fn(),
  queryDatabase: vi.fn(),
}));

import {
  getDatabaseTools,
  setupPgAdmin,
  type DatabaseTools,
  getDatabaseTables,
  queryDatabase,
  type DatabaseQueryResult,
} from '@/lib/api/adminDatabase';
import AdminDatabasePage from './page';

const load = vi.mocked(getDatabaseTools);
const setup = vi.mocked(setupPgAdmin);
const loadTables = vi.mocked(getDatabaseTables);
const query = vi.mocked(queryDatabase);
const url = 'https://agents.example.org/agents/api/admin/agent/pgadmin/';
const tables = [
  {
    name: 'jobs',
    columns: [
      { name: 'id', data_type: 'uuid', nullable: false },
      { name: 'title', data_type: 'text', nullable: true },
      { name: 'user_id', data_type: 'bigint', nullable: false },
      { name: 'enabled', data_type: 'boolean', nullable: false },
      { name: 'created_at', data_type: 'timestamp with time zone', nullable: false },
    ],
    primary_key: ['id'],
  },
  {
    name: 'hosts',
    columns: [{ name: 'id', data_type: 'uuid', nullable: false }],
    primary_key: ['id'],
  },
];
const result: DatabaseQueryResult = {
  table: 'jobs',
  columns: ['id', 'title', 'user_id'],
  rows: [['job-one', '<img src=x onerror=alert(1)>', '9223372036854775807']],
  limit: 50,
  offset: 0,
  has_more: true,
  elapsed_ms: 1.2,
  text_limit: 1000,
};

const waiting = { enabled: true, available: false, url: null, setup_required: true };

function fillSetup(confirmation = 'chosen-password-42') {
  fireEvent.change(screen.getByLabelText('Administrator email'), {
    target: { value: 'admin@example.org' },
  });
  fireEvent.change(screen.getByLabelText('Password', { exact: true }), {
    target: { value: 'chosen-password-42' },
  });
  fireEvent.change(screen.getByLabelText('Confirm password'), { target: { value: confirmation } });
  fireEvent.click(screen.getByRole('button', { name: 'Set up pgAdmin' }));
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.id = 'admin-a';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  load.mockResolvedValue({ enabled: true, available: true, url });
  loadTables.mockResolvedValue({ tables });
  query.mockResolvedValue(result);
});
afterEach(cleanup);

describe('Admin Database', () => {
  it('links to pgAdmin and selects Database in the existing admin navigation', async () => {
    render(<AdminDatabasePage />);
    const link = await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(link).toHaveAttribute('href', url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByRole('link', { name: 'Database' })).toHaveAttribute('aria-current', 'page');
  });

  it('does not contact or expose pgAdmin to a non-admin', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminDatabasePage />);
    expect(screen.getByText('Admin access required.')).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
    expect(loadTables).not.toHaveBeenCalled();
    expect(query).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('inspects columns and queries with text filters, null operators, and sorting', async () => {
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    expect(screen.getByLabelText('Rows per page')).toHaveValue('50');
    expect(screen.getByText('bigint')).toBeInTheDocument();
    expect(query).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Column 1'), { target: { value: 'user_id' } });
    fireEvent.change(screen.getByLabelText('Value 1'), {
      target: { value: '9223372036854775807' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Column 2'), { target: { value: 'title' } });
    fireEvent.change(screen.getByLabelText('Operator 2'), { target: { value: 'is_null' } });
    expect(screen.queryByLabelText('Value 2')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'id' } });
    fireEvent.change(screen.getByLabelText('Sort direction'), { target: { value: 'desc' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByRole('table', { name: 'Query results for jobs' });
    expect(query).toHaveBeenCalledWith(
      {
        table: 'jobs',
        filters: [
          { column: 'user_id', operator: 'eq', value: '9223372036854775807' },
          { column: 'title', operator: 'is_null' },
        ],
        order_by: 'id',
        order_direction: 'desc',
        limit: 50,
        offset: 0,
      },
      expect.any(AbortSignal),
    );
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText(/Text cells are limited to 1000 characters/)).toBeInTheDocument();
  });

  it('preserves submitted filters when moving forward and backward between pages', async () => {
    query
      .mockResolvedValueOnce(result)
      .mockResolvedValueOnce({ ...result, offset: 50, has_more: false })
      .mockResolvedValueOnce(result);
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Column 1'), { target: { value: 'title' } });
    fireEvent.change(screen.getByLabelText('Operator 1'), { target: { value: 'contains' } });
    fireEvent.change(screen.getByLabelText('Value 1'), { target: { value: 'worker%_one' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByRole('table', { name: 'Query results for jobs' });
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByText(/Rows 51–51/);
    expect(query.mock.calls[1][0]).toEqual({ ...query.mock.calls[0][0], offset: 50 });
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Previous page' }));
    await screen.findByText(/Rows 1–1/);
    expect(query.mock.calls[2][0]).toEqual(query.mock.calls[0][0]);
  });

  it('offers operators for the column type and explains boolean and timestamp inputs', async () => {
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    const operator = screen.getByLabelText('Operator 1');
    expect(within(operator).queryByRole('option', { name: 'Contains' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Column 1'), { target: { value: 'title' } });
    expect(within(operator).getByRole('option', { name: 'Contains' })).toBeInTheDocument();
    expect(
      within(operator).queryByRole('option', { name: 'Greater than' }),
    ).not.toBeInTheDocument();
    fireEvent.change(operator, { target: { value: 'contains' } });
    fireEvent.change(screen.getByLabelText('Column 1'), { target: { value: 'user_id' } });
    expect(operator).toHaveValue('eq');
    expect(within(operator).getByRole('option', { name: 'Greater than' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Column 1'), { target: { value: 'enabled' } });
    expect(screen.getByText('Use true or false.')).toBeInTheDocument();
    expect(
      within(operator).queryByRole('option', { name: 'Greater than' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Column 1'), { target: { value: 'created_at' } });
    expect(screen.getByText(/Use an ISO timestamp with a timezone/)).toBeInTheDocument();
  });

  it('retries failed schema and row requests and shows an empty query result', async () => {
    loadTables.mockRejectedValueOnce(new Error('Tables unavailable'));
    query
      .mockRejectedValueOnce(new Error('Query temporarily unavailable'))
      .mockResolvedValueOnce({ ...result, rows: [], has_more: false });
    render(<AdminDatabasePage />);
    await screen.findByText('Tables unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading tables' }));
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByText('Query temporarily unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry query' }));
    await screen.findByText(/No rows matched your query/);
    expect(query.mock.calls[1][0]).toEqual(query.mock.calls[0][0]);
    expect(screen.queryByRole('table', { name: 'Query results for jobs' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
  });

  it('explains an empty table catalog', async () => {
    loadTables.mockResolvedValue({ tables: [] });
    render(<AdminDatabasePage />);
    await screen.findByText('No tables are available to browse.');
    expect(screen.queryByRole('button', { name: 'Run query' })).not.toBeInTheDocument();
  });

  it('offers refinement when the server pagination boundary is reached', async () => {
    query.mockResolvedValue({ ...result, offset: 100_000, has_more: true });
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByText(/Pagination limit reached/);
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeEnabled();
  });

  it('aborts pending rows when the selected table changes and ignores stale completion', async () => {
    let resolveOld: (value: DatabaseQueryResult) => void = () => {};
    query.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    expect(screen.getByRole('button', { name: 'Run query' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    expect(query).toHaveBeenCalledTimes(1);
    const oldSignal = query.mock.calls[0][1];
    fireEvent.change(screen.getByLabelText('Table'), { target: { value: 'hosts' } });
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.getByRole('button', { name: 'Run query' })).toBeEnabled();
    await act(async () => resolveOld(result));
    expect(screen.queryByText('job-one')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    expect(query.mock.calls[1][0]).toMatchObject({ table: 'hosts', filters: [], offset: 0 });
  });

  it('clears results on filter or page-size changes and resets pagination', async () => {
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByText('job-one');
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    expect(screen.queryByText('job-one')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Rows per page'), { target: { value: '200' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    expect(query.mock.calls[1][0]).toMatchObject({ limit: 200, offset: 0 });
  });

  it.each([401, 403])('clears all admin data when a query returns %i', async (status) => {
    query.mockRejectedValue(new APIError('FORBIDDEN', 'Permission revoked', status));
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByText('Admin access required.');
    expect(screen.queryByLabelText('Table')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('aborts a pending row query when another admin endpoint denies access', async () => {
    let rejectTools: (cause: unknown) => void = () => {};
    let resolveRows: (value: DatabaseQueryResult) => void = () => {};
    load.mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectTools = reject;
      }),
    );
    query.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveRows = resolve;
      }),
    );
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    const rowSignal = query.mock.calls[0][1];
    await act(async () => rejectTools(new APIError('FORBIDDEN', 'Permission revoked', 403)));
    await screen.findByText('Admin access required.');
    expect(rowSignal?.aborted).toBe(true);
    await act(async () => resolveRows(result));
    expect(screen.queryByText('job-one')).not.toBeInTheDocument();
  });

  it('aborts a pending row query on account change and ignores the old rows', async () => {
    let resolveOld: (value: DatabaseQueryResult) => void = () => {};
    query.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const view = render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    const oldSignal = query.mock.calls[0][1];
    auth.id = 'admin-b';
    view.rerender(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => resolveOld(result));
    expect(screen.queryByText('job-one')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run query' })).toBeEnabled();
  });

  it('aborts pending database work when the client permission is revoked', async () => {
    query.mockReturnValueOnce(new Promise(() => {}));
    const view = render(<AdminDatabasePage />);
    await screen.findByLabelText('Table');
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    const oldSignal = query.mock.calls[0][1];
    auth.access = { allowed: true, permissions: ['agent.use'] };
    view.rerender(<AdminDatabasePage />);
    expect(screen.getByText('Admin access required.')).toBeInTheDocument();
    expect(oldSignal?.aborted).toBe(true);
  });

  it('loads successfully through StrictMode effect cleanup and restart', async () => {
    render(
      <StrictMode>
        <AdminDatabasePage />
      </StrictMode>,
    );
    await screen.findByLabelText('Table');
    expect(loadTables.mock.calls[0][0]?.aborted).toBe(true);
    expect(loadTables.mock.calls[1][0]?.aborted).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByText('job-one');
  });

  it('explains disabled deployments without showing a dead link', async () => {
    load.mockResolvedValue({ enabled: false, available: false, url: null });
    render(<AdminDatabasePage />);
    await screen.findByText(/pgAdmin has not been enabled/);
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('rechecks an unavailable service and enables the working launch link', async () => {
    load.mockResolvedValueOnce({ enabled: true, available: false, url: null });
    render(<AdminDatabasePage />);
    await screen.findByText(/pgAdmin is temporarily unavailable/);
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('removes access when the server rejects the current permission', async () => {
    load.mockRejectedValue(new APIError('FORBIDDEN', 'Permission revoked', 403));
    render(<AdminDatabasePage />);
    await screen.findByText('Admin access required.');
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('aborts the old request and ignores its result when the signed-in user changes', async () => {
    let resolveOld: (value: {
      enabled: boolean;
      available: boolean;
      url: string;
    }) => void = () => {};
    load.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const view = render(<AdminDatabasePage />);
    const oldSignal = load.mock.calls[0][0];
    auth.id = 'admin-b';
    view.rerender(<AdminDatabasePage />);
    await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(oldSignal?.aborted).toBe(true);
    resolveOld({ enabled: true, available: true, url: 'https://old.example.org/' });
    await waitFor(() =>
      expect(screen.getByRole('link', { name: /Open pgAdmin/ })).toHaveAttribute('href', url),
    );
  });

  it('allows the first admin to choose credentials and polls until pgAdmin starts', async () => {
    load
      .mockResolvedValueOnce(waiting)
      .mockResolvedValueOnce({ ...waiting, setup_required: false });
    setup.mockResolvedValue({ enabled: true, available: false, url: null, setup_required: false });
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Administrator email');
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
    fillSetup();
    await screen.findByText(/Starting pgAdmin/);
    expect(setup).toHaveBeenCalledWith(
      'admin@example.org',
      'chosen-password-42',
      expect.any(AbortSignal),
    );
    expect(screen.queryByLabelText('Password', { exact: true })).not.toBeInTheDocument();
    await screen.findByRole('link', { name: /Open pgAdmin/ }, { timeout: 5000 });
  });

  it('requires matching passwords before submitting credentials', async () => {
    load.mockResolvedValue(waiting);
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Administrator email');
    fillSetup('different-password');
    expect(screen.getByRole('alert')).toHaveTextContent('The passwords must match.');
    expect(setup).not.toHaveBeenCalled();
  });

  it('rechecks status when another administrator has already completed setup', async () => {
    load.mockResolvedValueOnce(waiting);
    setup.mockRejectedValue(new APIError('CONFLICT', 'Already configured', 409));
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Administrator email');
    fillSetup();
    await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('removes the setup form when admin access is revoked during submission', async () => {
    load.mockResolvedValue(waiting);
    setup.mockRejectedValue(new APIError('FORBIDDEN', 'Permission revoked', 403));
    render(<AdminDatabasePage />);
    await screen.findByLabelText('Administrator email');
    fillSetup();
    await screen.findByText('Admin access required.');
    expect(screen.queryByLabelText('Administrator email')).not.toBeInTheDocument();
  });

  it('aborts pending setup and clears credentials when the signed-in user changes', async () => {
    load.mockResolvedValue(waiting);
    let resolveOld: (value: DatabaseTools) => void = () => {};
    setup.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const view = render(<AdminDatabasePage />);
    await screen.findByLabelText('Administrator email');
    fillSetup();
    const oldSignal = setup.mock.calls[0][2];
    auth.id = 'admin-b';
    view.rerender(<AdminDatabasePage />);
    await screen.findByLabelText('Administrator email');
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.getByLabelText('Administrator email')).toHaveValue('');
    expect(screen.getByLabelText('Password', { exact: true })).toHaveValue('');
    resolveOld({ enabled: true, available: true, url: 'https://old.example.org/' });
    await waitFor(() =>
      expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument(),
    );
  });
});
