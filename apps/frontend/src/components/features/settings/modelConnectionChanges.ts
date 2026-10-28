import type { ModelConnection, ModelConnectionInput } from '@/lib/api/modelConnections';

/** What the form holds. */
export interface ConnectionFields {
  name: string;
  base_url: string;
  models: string[];
  key: string;
}

/** One typed model ID, trimmed; `''` adds nothing. */
export function cleanModel(text: string): string {
  return text.trim();
}

/** `models` with `model` appended once, in order. */
export function withModel(models: readonly string[], model: string): string[] {
  const clean = cleanModel(model);
  return !clean || models.includes(clean) ? [...models] : [...models, clean];
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function sameBaseUrl(typed: string, stored: string): boolean {
  // The server drops trailing slashes; the same address typed with one is unchanged.
  return typed.replace(/\/+$/, '') === stored;
}

/**
 * Only what the owner actually changed. A rename sends the name alone, so the
 * connection's key, its running agent processes and every model's
 * verification stay as they were. A blank key keeps the stored one.
 */
export function connectionChanges(
  connection: ModelConnection,
  fields: ConnectionFields,
): Partial<ModelConnectionInput> {
  const changes: Partial<ModelConnectionInput> = {};
  if (fields.name !== connection.name) changes.name = fields.name;
  if (!sameBaseUrl(fields.base_url, connection.base_url)) changes.base_url = fields.base_url;
  if (!sameList(fields.models, connection.models)) changes.models = fields.models;
  if (fields.key) changes.api_key = fields.key;
  return changes;
}

/**
 * Whether saving calls the provider: a new connection, a new key or Base URL,
 * or an added model. A rename, removing models or no change does not.
 */
export function needsVerification(
  connection: ModelConnection | undefined,
  fields: ConnectionFields,
): boolean {
  if (!connection) return true;
  const changes = connectionChanges(connection, fields);
  if (changes.base_url !== undefined || changes.api_key !== undefined) return true;
  return fields.models.some((model) => !connection.models.includes(model));
}

export function newConnection(fields: ConnectionFields): ModelConnectionInput {
  return {
    name: fields.name,
    base_url: fields.base_url,
    api_key: fields.key,
    models: fields.models,
  };
}
