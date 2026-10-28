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

/** Client protocols the helper can run on a Chat Completions connection. */
const TRANSLATED_CLIENTS: readonly ModelProtocol[] = ['responses', 'anthropic_messages'];

export interface CheckTarget {
  protocol: ModelProtocol;
  translated: boolean;
}

/**
 * What a connection can be tested as: each protocol it declares, natively,
 * then each client protocol it serves only through translation. Native first,
 * as a task would run; the declared protocols are never rewritten.
 */
export function checkTargets(protocols: readonly ModelProtocol[]): CheckTarget[] {
  const native = protocols.map((protocol) => ({ protocol, translated: false }));
  if (!protocols.includes('chat_completions')) return native;
  const translated = TRANSLATED_CLIENTS.filter((client) => !protocols.includes(client));
  return [...native, ...translated.map((protocol) => ({ protocol, translated: true }))];
}

/** A protocol's proper name, the same in every language. */
export function protocolName(protocol: ModelProtocol): string {
  return PROTOCOL_NAMES[protocol] ?? protocol;
}

export interface ModelConnectionCheck {
  status: string;
  category: string;
  message: string;
  /** The client protocol tested: what the agent's CLI speaks. */
  protocol: ModelProtocol;
  /** What the request went out as; differs from `protocol` when translated. */
  upstream_protocol?: ModelProtocol;
  mode?: 'direct' | 'translated';
  model: string;
  streaming: boolean;
  tool_call: boolean;
  final_answer: boolean;
  http_status: number | null;
  checked_at: string;
  recorded: boolean;
}

export interface ModelConnection {
  id: string;
  name: string;
  base_url: string;
  protocols: ModelProtocol[];
  models: string[];
  enabled: boolean;
  has_api_key: boolean;
  runtimes: string[];
  /** Agents that reach this connection through the compatibility helper. */
  translated_runtimes?: string[];
  last_check: ModelConnectionCheck | null;
  created_at: string;
  updated_at: string;
}

export interface ModelConnectionInput {
  name: string;
  base_url: string;
  api_key: string;
  protocols: ModelProtocol[];
  models: string[];
}

export interface ModelConnectionList {
  configured: boolean;
  enabled: boolean;
  connections: ModelConnection[];
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

export async function checkModelConnection(
  id: string,
  model: string,
  protocol: ModelProtocol,
): Promise<ModelConnectionCheck> {
  return jsonOrThrow(
    await fetchWithAuth(config.apiBase, path(id, '/check'), json('POST', { model, protocol })),
  );
}

export async function discoverConnectionModels(id: string): Promise<string[]> {
  const response = await fetchWithAuth(config.apiBase, path(id, '/discover-models'), {
    method: 'POST',
  });
  return (await jsonOrThrow<{ models: string[] }>(response)).models;
}
