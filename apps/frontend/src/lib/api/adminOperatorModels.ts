import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';
import { connectionJson, MAX_MODELS, type ModelStatus } from './modelConnections';

// Models the deployment provides, as an administrator sets them up.
//
// A provided connection (`oconn_…`) is the deployment's own inference
// endpoint, offered to every admitted account with no setup of theirs. Its
// key is write-only, and every account has a daily request allowance per
// model. What an owner is offered is `ProvidedModelSourceApi` in `/config`.
//
// Refusals mirror the owner's connection API, so `refusedModels` and
// `invalidFields` from `./modelConnections` read them too.

const ROOT = '/admin/agent/operator-model-connections';

/** How many models one provided connection may list: the same as an owner's. */
export const OPERATOR_MAX_MODELS = MAX_MODELS;
/** The longest name the server keeps for a connection. */
export const OPERATOR_MAX_NAME_CHARS = 64;
/** The most an allowance is ever set to. */
export const MAX_DAILY_REQUESTS = 10_000_000;
/** The most a deployment-wide concurrency cap is ever set to. */
export const MAX_GLOBAL_CONCURRENCY = 1024;
/** The most requests one account may have in flight at once. */
export const MAX_USER_CONCURRENCY = 64;
export const DEFAULT_USER_CONCURRENCY = 4;

/** One model's default allowance, and its deployment-wide concurrency cap. */
export interface OperatorModelLimit {
  /**
   * Requests each account may send per UTC day. `null` is unlimited; `0`
   * offers the model only to accounts given an allowance of their own.
   */
  user_daily_requests: number | null;
  /** Requests in flight across all accounts; `null` is unlimited. */
  global_concurrency: number | null;
}

/** One provided connection, as an administrator reads it. Never its key. */
export interface OperatorModelConnection {
  id: string;
  name: string;
  base_url: string;
  models: string[];
  default_model: string | null;
  /** Whether a new account with no history starts here. */
  is_default: boolean;
  enabled: boolean;
  /** The header each relayed request names its account in, by an opaque tag. */
  end_user_header: string | null;
  /** Requests one account may have in flight at once. */
  user_concurrency: number;
  /**
   * Each model's default allowance. A model absent here has none set yet, and
   * the connection cannot be enabled until every model has one.
   */
  limits: Record<string, OperatorModelLimit>;
  model_status: ModelStatus[];
  /** Since when the provider has refused the stored key; `null` is healthy. */
  key_refused_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface OperatorModelConnectionList {
  connections: OperatorModelConnection[];
  /** Enrolled servers that reach the model relay; zero means no turn can run on one. */
  relay_hosts: number;
  /** Whether model connections are switched on at all on this deployment. */
  feature_enabled: boolean;
}

export interface OperatorModelConnectionCreate {
  name: string;
  base_url: string;
  api_key: string;
  models: string[];
  default_model?: string | null;
  end_user_header?: string | null;
  user_concurrency?: number;
}

/** An edit: a field left out is kept. `''` clears `default_model` and `end_user_header`. */
export interface OperatorModelConnectionUpdate {
  name?: string;
  base_url?: string;
  api_key?: string;
  models?: string[];
  default_model?: string;
  is_default?: boolean;
  enabled?: boolean;
  end_user_header?: string;
  user_concurrency?: number;
}

/** One account with an allowance of its own, or with use today. */
export interface OperatorModelUser {
  account_id: string;
  email: string;
  /** The account's own allowances; a model absent here follows the default. */
  limits: Record<string, number | null>;
  /** Requests accepted today, per model. */
  used_today: Record<string, number>;
}

export interface OperatorModelUserList {
  users: OperatorModelUser[];
  next_cursor: string | null;
}

/** Requests the relay accepted from one account for one model on one UTC day. */
export interface OperatorModelUsageRow {
  day: string;
  account_id: string;
  email: string;
  model: string;
  requests: number;
}

export interface OperatorModelUsage {
  rows: OperatorModelUsageRow[];
  /** More rows matched than are answered. */
  truncated: boolean;
}

function path(id: string, suffix = '') {
  return `${ROOT}/${encodeURIComponent(id)}${suffix}`;
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

async function done(response: Response): Promise<void> {
  if (!response.ok) await jsonOrThrow(response);
}

export async function listOperatorModelConnections(
  signal?: AbortSignal,
): Promise<OperatorModelConnectionList> {
  return jsonOrThrow(await fetchWithAuth(config.apiBase, ROOT, { signal, cache: 'no-store' }));
}

/**
 * Verify every model with the provider, then save the connection, disabled.
 * This can take minutes; a refusal stores nothing (`refusedModels`).
 */
export async function createOperatorModelConnection(
  input: OperatorModelConnectionCreate,
): Promise<OperatorModelConnection> {
  return connectionJson(await fetchWithAuth(config.apiBase, ROOT, json('POST', input)));
}

/**
 * Save changes. A new key or Base URL, or an added model, is verified first;
 * a refusal leaves the connection as it was.
 */
export async function updateOperatorModelConnection(
  id: string,
  changes: OperatorModelConnectionUpdate,
): Promise<OperatorModelConnection> {
  return connectionJson(await fetchWithAuth(config.apiBase, path(id), json('PATCH', changes)));
}

export async function deleteOperatorModelConnection(id: string): Promise<void> {
  await done(await fetchWithAuth(config.apiBase, path(id), { method: 'DELETE' }));
}

/** Set one model's default allowance and its deployment-wide concurrency cap. */
export async function setOperatorModelLimit(
  id: string,
  limit: { model: string } & OperatorModelLimit,
): Promise<OperatorModelConnection> {
  return connectionJson(
    await fetchWithAuth(config.apiBase, path(id, '/limits'), json('PUT', limit)),
  );
}

/** Accounts with an allowance of their own or with use today, a page at a time. */
export async function listOperatorModelUsers(
  id: string,
  options: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<OperatorModelUserList> {
  const query = new URLSearchParams();
  if (options.cursor) query.set('cursor', options.cursor);
  if (options.limit) query.set('limit', String(options.limit));
  const suffix = query.size ? `/users?${query}` : '/users';
  return jsonOrThrow(
    await fetchWithAuth(config.apiBase, path(id, suffix), {
      signal: options.signal,
      cache: 'no-store',
    }),
  );
}

/** Give one account, named by id or email, an allowance of its own on one model. */
export async function setOperatorModelUserLimit(
  id: string,
  limit: { account: string; model: string; daily_requests: number | null },
): Promise<OperatorModelUser> {
  return connectionJson(
    await fetchWithAuth(config.apiBase, path(id, '/user-limits'), json('PUT', limit)),
  );
}

/** Put one account back on a model's default allowance. */
export async function clearOperatorModelUserLimit(
  id: string,
  accountId: string,
  model: string,
): Promise<void> {
  const query = new URLSearchParams({ account_id: accountId, model });
  await done(
    await fetchWithAuth(config.apiBase, path(id, `/user-limits?${query}`), { method: 'DELETE' }),
  );
}

/** Accepted requests per UTC day, account and model, between two `YYYY-MM-DD` days. */
export async function getOperatorModelUsage(
  id: string,
  range: { from: string; to: string },
  signal?: AbortSignal,
): Promise<OperatorModelUsage> {
  const query = new URLSearchParams({ from: range.from, to: range.to });
  return jsonOrThrow(
    await fetchWithAuth(config.apiBase, path(id, `/usage?${query}`), { signal, cache: 'no-store' }),
  );
}
