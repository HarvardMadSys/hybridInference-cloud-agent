import { agentApiBase } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface DatabaseTools {
  enabled: boolean;
  available: boolean;
  url: string | null;
  setup_required?: boolean;
}

export async function setupPgAdmin(
  email: string,
  password: string,
  signal?: AbortSignal,
): Promise<DatabaseTools> {
  return jsonOrThrow<DatabaseTools>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database-tools/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      signal,
      cache: 'no-store',
    }),
  );
}

export interface DatabaseColumn {
  name: string;
  data_type: string;
  nullable: boolean;
}

export interface DatabaseTable {
  name: string;
  columns: DatabaseColumn[];
  primary_key: string[];
}

export interface DatabaseTables {
  tables: DatabaseTable[];
}

export type DatabaseFilterOperator =
  | 'eq'
  | 'ne'
  | 'contains'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'is_null'
  | 'is_not_null';

export interface DatabaseFilter {
  column: string;
  operator: DatabaseFilterOperator;
  value?: string | null;
}

export interface DatabaseQuery {
  table: string;
  filters: DatabaseFilter[];
  order_by?: string | null;
  order_direction: 'asc' | 'desc';
  limit: number;
  offset: number;
}

export interface DatabaseQueryResult {
  table: string;
  columns: string[];
  rows: unknown[][];
  limit: number;
  offset: number;
  has_more: boolean;
  elapsed_ms: number;
  text_limit: number;
}

export async function getDatabaseTools(signal?: AbortSignal): Promise<DatabaseTools> {
  return jsonOrThrow<DatabaseTools>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database-tools', { signal, cache: 'no-store' }),
  );
}

export async function getDatabaseTables(signal?: AbortSignal): Promise<DatabaseTables> {
  return jsonOrThrow<DatabaseTables>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database/tables', {
      signal,
      cache: 'no-store',
    }),
  );
}

export async function queryDatabase(
  query: DatabaseQuery,
  signal?: AbortSignal,
): Promise<DatabaseQueryResult> {
  return jsonOrThrow<DatabaseQueryResult>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(query),
      signal,
      cache: 'no-store',
    }),
  );
}
