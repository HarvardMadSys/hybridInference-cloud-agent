import { afterEach, expect, it, vi } from 'vitest';
import {
  getDatabaseTables,
  getDatabaseTools,
  queryDatabase,
  type DatabaseQuery,
} from './adminDatabase';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

it('uses uncached admin endpoints and forwards cancellation for schema, queries, and pgAdmin', async () => {
  const fetch = vi
    .spyOn(client, 'fetchWithAuth')
    .mockImplementation(
      async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    );
  const controller = new AbortController();
  await getDatabaseTables(controller.signal);
  expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/admin/agent/database/tables', {
    signal: controller.signal,
    cache: 'no-store',
  });
  const query: DatabaseQuery = {
    table: 'jobs',
    filters: [
      { column: 'user_id', operator: 'eq', value: '9223372036854775807' },
      { column: 'deleted_at', operator: 'is_null' },
    ],
    order_by: 'created_at',
    order_direction: 'desc',
    limit: 50,
    offset: 100,
  };
  await queryDatabase(query, controller.signal);
  expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/admin/agent/database/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(query),
    signal: controller.signal,
    cache: 'no-store',
  });
  await getDatabaseTools(controller.signal);
  expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/admin/agent/database-tools', {
    signal: controller.signal,
    cache: 'no-store',
  });
});

it('surfaces query permission denial with its status for clearing the admin UI', async () => {
  vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
    new Response(JSON.stringify({ detail: 'Admin access required' }), {
      status: 403,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
  await expect(
    queryDatabase({ table: 'jobs', filters: [], order_direction: 'asc', limit: 50, offset: 0 }),
  ).rejects.toMatchObject({ statusCode: 403 });
});
