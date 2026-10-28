import { config } from '@/config/env';
import { APIError } from '@/lib/utils/errors';
import { fetchWithAuth, jsonOrThrow } from './client';

export type ModelProtocol = 'chat_completions' | 'responses' | 'anthropic_messages';

const PROTOCOL_NAMES: Record<ModelProtocol, string> = {
  chat_completions: 'Chat Completions',
  responses: 'Responses',
  anthropic_messages: 'Anthropic Messages',
};

/** A protocol's proper name, the same in every language. Technical details only. */
export function protocolName(protocol: ModelProtocol): string {
  return PROTOCOL_NAMES[protocol] ?? protocol;
}

/**
 * One model's state. `ready`: verified, with the agents in `runtimes`;
 * `failed`: the check could not determine anything (unreachable, key refused,
 * rate limited…); `unsupported`: the provider answered and no API completed a
 * streaming tool call. A saved model is always `ready`; `unverified` is a
 * backstop the server should never answer for one.
 */
export type ModelStatusKind = 'ready' | 'unverified' | 'failed' | 'unsupported';

/** What one native API's check found for a model. Technical details only. */
export interface ProtocolResult {
  status: string;
  category: string;
  message: string;
  http_status: number | null;
}

/** How one agent reaches a model. Technical details only. */
export interface AgentPath {
  runtime: string;
  mode: 'direct' | 'translated';
  client_protocol: ModelProtocol;
  upstream_protocol: ModelProtocol;
}

export interface ModelStatus {
  model: string;
  status: ModelStatusKind;
  category: string | null;
  message: string;
  /** The agents a task may run this model with: what the server accepts. */
  runtimes: string[];
  paths: AgentPath[];
  protocols: Partial<Record<ModelProtocol, ProtocolResult>>;
  checked_at: string | null;
}

export interface ModelConnection {
  id: string;
  name: string;
  base_url: string;
  models: string[];
  enabled: boolean;
  has_api_key: boolean;
  /** Every agent at least one listed model can run with. */
  runtimes: string[];
  model_status: ModelStatus[];
  created_at: string;
  updated_at: string;
}

export interface ModelConnectionInput {
  name: string;
  base_url: string;
  api_key: string;
  models: string[];
}

export interface ModelConnectionList {
  configured: boolean;
  enabled: boolean;
  /** How many models one owner may verify per hour. */
  checks_per_hour?: number;
  /** How many models one connection may list. */
  max_models?: number;
  connections: ModelConnection[];
}

/** The server's per-connection limit, when an older answer does not say. */
export const MAX_MODELS = 8;

/** A save refused because a model could not be verified: nothing was stored. */
export const VERIFICATION_FAILED = 'model_connection_verification_failed';

/** Each verified model's result, from a refused save; `null` for any other failure. */
export function refusedModels(cause: unknown): ModelStatus[] | null {
  if (!(cause instanceof APIError) || cause.code !== VERIFICATION_FAILED) return null;
  const models = cause.details?.models;
  return Array.isArray(models) ? (models as ModelStatus[]) : null;
}

/**
 * `jsonOrThrow`, keeping the per-model results a refused save carries; the
 * shared error parser keeps only the type and the message.
 */
async function connectionJson<T>(resp: Response): Promise<T> {
  if (resp.ok) return resp.json() as Promise<T>;
  let models: unknown = null;
  try {
    const body = (await resp.clone().json()) as { detail?: { error?: { models?: unknown } } };
    models = body?.detail?.error?.models ?? null;
  } catch {
    // Not JSON: jsonOrThrow classifies it by status.
  }
  try {
    return await jsonOrThrow<T>(resp);
  } catch (cause) {
    if (cause instanceof APIError && Array.isArray(models)) {
      cause.details = { ...(cause.details ?? {}), models };
    }
    throw cause;
  }
}

const ROOT = '/v1/agent/model-connections';
function path(id: string, suffix = '') {
  return `${ROOT}/${encodeURIComponent(id)}${suffix}`;
}
function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

export async function listModelConnections(): Promise<ModelConnectionList> {
  return jsonOrThrow(await fetchWithAuth(config.apiBase, ROOT, { cache: 'no-store' }));
}

/** Verify every model, then save; a refusal stores nothing (`refusedModels`). */
export async function createModelConnection(input: ModelConnectionInput): Promise<ModelConnection> {
  return connectionJson(await fetchWithAuth(config.apiBase, ROOT, json('POST', input)));
}

/**
 * Save changes. A new key or Base URL, or an added model, is verified first;
 * a refusal leaves the connection as it was (`refusedModels`).
 */
export async function updateModelConnection(
  id: string,
  changes: Partial<ModelConnectionInput> & { enabled?: boolean },
): Promise<ModelConnection> {
  return connectionJson(await fetchWithAuth(config.apiBase, path(id), json('PATCH', changes)));
}

export async function deleteModelConnection(id: string): Promise<void> {
  const response = await fetchWithAuth(config.apiBase, path(id), { method: 'DELETE' });
  if (!response.ok) await jsonOrThrow(response);
}

export async function discoverConnectionModels(id: string): Promise<string[]> {
  const response = await fetchWithAuth(config.apiBase, path(id, '/discover-models'), {
    method: 'POST',
  });
  return (await jsonOrThrow<{ models: string[] }>(response)).models;
}
