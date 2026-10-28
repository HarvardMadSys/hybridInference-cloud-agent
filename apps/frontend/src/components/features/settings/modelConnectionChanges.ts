import type {
  ModelConnection,
  ModelConnectionInput,
  ModelProtocol,
} from '@/lib/api/modelConnections';

/** What the form holds: the four ordinary fields and the advanced declaration. */
export interface ConnectionFields {
  name: string;
  base_url: string;
  models: string[];
  key: string;
  /** Empty is automatic detection. */
  protocols: ModelProtocol[];
}

/** The model IDs typed one per line: trimmed, de-duplicated, in order. */
export function parseModels(text: string): string[] {
  return [
    ...new Set(
      text
        .split('\n')
        .map((model) => model.trim())
        .filter(Boolean),
    ),
  ];
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function sameBaseUrl(typed: string, stored: string): boolean {
  // The server drops one trailing slash; the same address typed with it is unchanged.
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
  if (!sameList(fields.protocols, connection.manual_protocols))
    changes.protocols = fields.protocols;
  if (fields.key) changes.api_key = fields.key;
  return changes;
}

/** A new connection's request: automatic unless the owner declared APIs. */
export function newConnection(fields: ConnectionFields): ModelConnectionInput {
  return {
    name: fields.name,
    base_url: fields.base_url,
    api_key: fields.key,
    models: fields.models,
    ...(fields.protocols.length ? { protocols: fields.protocols } : {}),
  };
}
