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

/** A protocol's proper name, the same in every language. */
export function protocolName(protocol: ModelProtocol): string {
  return PROTOCOL_NAMES[protocol] ?? protocol;
}

export interface ModelConnectionCheck {
  status: string;
  category: string;
  message: string;
  protocol: ModelProtocol;
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
