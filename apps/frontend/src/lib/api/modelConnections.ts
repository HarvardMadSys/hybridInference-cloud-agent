import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export type ModelProtocol = 'chat_completions' | 'responses' | 'anthropic_messages';

/** Every protocol a connection may declare, in the platform's order. */
export const MODEL_PROTOCOLS: readonly ModelProtocol[] = [
  'chat_completions',
  'responses',
  'anthropic_messages',
];

const PROTOCOL_NAMES: Record<ModelProtocol, string> = {
  chat_completions: 'Chat Completions',
  responses: 'Responses',
  anthropic_messages: 'Anthropic Messages',
};

/** A protocol's proper name, the same in every language. Diagnostics only. */
export function protocolName(protocol: ModelProtocol): string {
  return PROTOCOL_NAMES[protocol] ?? protocol;
}

/**
 * One listed model's state. `ready`: verified, with the agents in `runtimes`;
 * `declared`: the owner declared the APIs manually; `unverified`: not checked
 * against the current key and Base URL; `failed`: the check could not
 * determine anything (unreachable, key refused, rate limited…); `unsupported`:
 * the provider answered and no API completed a streaming tool call.
 */
export type ModelStatusKind = 'ready' | 'declared' | 'unverified' | 'failed' | 'unsupported';

/** What one native API's check found for a model. Diagnostics only. */
export interface ProtocolResult {
  status: string;
  category: string;
  message: string;
  http_status: number | null;
}

/** How one agent reaches a model. Diagnostics only. */
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
  /** The agents a task may run this model with now: what the server accepts. */
  runtimes: string[];
  paths: AgentPath[];
  protocols: Partial<Record<ModelProtocol, ProtocolResult>>;
  checked_at: string | null;
}

export interface ModelVerification extends ModelStatus {
  /** False when the connection changed while it ran; nothing was kept. */
  recorded: boolean;
}

export interface ModelConnection {
  id: string;
  name: string;
  base_url: string;
  /** `auto`: each model's APIs come from verifying it; `manual`: declared. */
  protocol_mode: 'auto' | 'manual';
  manual_protocols: ModelProtocol[];
  models: string[];
  enabled: boolean;
  has_api_key: boolean;
  /** Every agent at least one listed model can run with now. */
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
  /** Advanced: declared APIs; empty (or omitted) is automatic detection. */
  protocols?: ModelProtocol[];
}

export interface ModelConnectionList {
  configured: boolean;
  enabled: boolean;
  /** How many models one owner may verify per hour. */
  checks_per_hour?: number;
  connections: ModelConnection[];
}

/** How many models one "verify" action checks, one after another. */
export const VERIFY_BATCH = 3;

/** The models a "verify" action should check: never verified, or stale. */
export function unverifiedModels(connection: ModelConnection): string[] {
  return connection.model_status
    .filter((status) => status.status === 'unverified')
    .map((status) => status.model);
}

/** `connection` with one model's kept verification applied, as the server now holds it. */
export function withVerification(
  connection: ModelConnection,
  verification: ModelVerification,
): ModelConnection {
  if (!verification.recorded) return connection;
  const { recorded: _recorded, ...status } = verification;
  const model_status = connection.model_status.map((item) =>
    item.model === status.model ? status : item,
  );
  const runtimes = [...new Set(model_status.flatMap((item) => item.runtimes))].sort();
  return { ...connection, model_status, runtimes };
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

export async function createModelConnection(input: ModelConnectionInput): Promise<ModelConnection> {
  return jsonOrThrow(await fetchWithAuth(config.apiBase, ROOT, json('POST', input)));
}

export async function updateModelConnection(
  id: string,
  changes: Partial<ModelConnectionInput> & { enabled?: boolean },
): Promise<ModelConnection> {
  return jsonOrThrow(await fetchWithAuth(config.apiBase, path(id), json('PATCH', changes)));
}

export async function deleteModelConnection(id: string): Promise<void> {
  const response = await fetchWithAuth(config.apiBase, path(id), { method: 'DELETE' });
  if (!response.ok) await jsonOrThrow(response);
}

/** Verify one model: a few billable provider requests, made only when asked. */
export async function verifyModelConnection(id: string, model: string): Promise<ModelVerification> {
  return jsonOrThrow(
    await fetchWithAuth(config.apiBase, path(id, '/verify'), json('POST', { model })),
  );
}

export async function discoverConnectionModels(id: string): Promise<string[]> {
  const response = await fetchWithAuth(config.apiBase, path(id, '/discover-models'), {
    method: 'POST',
  });
  return (await jsonOrThrow<{ models: string[] }>(response)).models;
}
