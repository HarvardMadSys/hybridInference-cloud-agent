/**
 * Check a request the API client built against the backend's published contract.
 *
 * `gateway_contracts/agent-control-plane.openapi.json` is the OpenAPI document
 * the control plane serves; `tests/unit/test_api_surface_contract.py` keeps the
 * committed copy identical to it. The client tests mock `fetchWithAuth`, so
 * without this a path the backend renamed, or a body field it no longer
 * accepts, passes every frontend test and fails only in the browser. Request
 * bodies are `additionalProperties: false` on the server, so a stray field is
 * a 422 there, and is an error here too.
 *
 * Deliberately shallow: it checks the route, the query names, and each body
 * field's presence and JSON type. Bounds and formats stay the server's job.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, vi } from 'vitest';
import { fetchWithAuth } from '../client';

type Schema = {
  $ref?: string;
  type?: string;
  anyOf?: Schema[];
  enum?: unknown[];
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: boolean | Schema;
  items?: Schema;
};

type Operation = {
  parameters?: { name: string; in: string; required?: boolean }[];
  requestBody?: { required?: boolean; content: Record<string, { schema: Schema }> };
};

type Document = {
  paths: Record<string, Record<string, Operation>>;
  components: { schemas: Record<string, Schema> };
};

const CONTRACT = resolve(
  __dirname,
  '../../../../../../gateway_contracts/agent-control-plane.openapi.json',
);

let contract: Document | null = null;

function document(): Document {
  contract ??= JSON.parse(readFileSync(CONTRACT, 'utf8')) as Document;
  return contract;
}

function resolveRef(schema: Schema): Schema {
  if (!schema.$ref) return schema;
  const name = schema.$ref.replace('#/components/schemas/', '');
  const found = document().components.schemas[name];
  if (!found) throw new Error(`the contract has no schema ${name}`);
  return resolveRef(found);
}

/** The contract path a concrete URL path is, preferring literal segments over parameters. */
function route(path: string): string | null {
  const segments = path.split('/');
  let best: { template: string; params: number } | null = null;
  for (const template of Object.keys(document().paths)) {
    const parts = template.split('/');
    if (parts.length !== segments.length) continue;
    let params = 0;
    const matches = parts.every((part, index) => {
      if (part.startsWith('{') && part.endsWith('}')) {
        params += 1;
        return segments[index] !== '';
      }
      return part === segments[index];
    });
    if (matches && (best === null || params < best.params)) best = { template, params };
  }
  return best?.template ?? null;
}

function typeErrors(value: unknown, raw: Schema, where: string): string[] {
  const schema = resolveRef(raw);
  if (schema.anyOf) {
    const fits = schema.anyOf.some((option) => typeErrors(value, option, where).length === 0);
    return fits ? [] : [`${where}: ${JSON.stringify(value)} fits none of its types`];
  }
  if (schema.enum && !schema.enum.includes(value)) {
    return [`${where}: ${JSON.stringify(value)} is not one of ${JSON.stringify(schema.enum)}`];
  }
  let actual: string = typeof value;
  if (value === null) actual = 'null';
  else if (Array.isArray(value)) actual = 'array';
  else if (Number.isInteger(value)) actual = 'integer';
  const expected = schema.type;
  if (expected === undefined) return [];
  if (expected === 'number' && actual === 'integer') return [];
  if (actual !== expected) return [`${where}: expected ${expected}, got ${actual}`];
  const items = schema.items;
  if (actual === 'array' && items) {
    return (value as unknown[]).flatMap((item, i) => typeErrors(item, items, `${where}[${i}]`));
  }
  if (actual === 'object') return objectErrors(value as Record<string, unknown>, schema, where);
  return [];
}

function objectErrors(value: Record<string, unknown>, schema: Schema, where: string): string[] {
  const properties = schema.properties ?? {};
  const errors = (schema.required ?? [])
    .filter((name) => !(name in value))
    .map((name) => `${where}.${name} is required`);
  for (const [name, field] of Object.entries(value)) {
    if (name in properties) {
      errors.push(...typeErrors(field, properties[name], `${where}.${name}`));
    } else if (schema.additionalProperties === false) {
      errors.push(`${where}.${name} is not a field the server accepts`);
    } else if (typeof schema.additionalProperties === 'object') {
      errors.push(...typeErrors(field, schema.additionalProperties, `${where}.${name}`));
    }
  }
  return errors;
}

/** Every way `method path` with `body` disagrees with the contract; empty when it agrees. */
export function contractErrors(method: string, url: string, body?: BodyInit | null): string[] {
  const [path, query = ''] = url.split('?', 2);
  const template = route(path);
  if (template === null) return [`no route in the contract matches ${path}`];
  const operation = document().paths[template][method.toLowerCase()];
  if (!operation) return [`${template} has no ${method.toUpperCase()}`];
  const errors: string[] = [];
  const queryNames = new Set(
    (operation.parameters ?? []).filter((p) => p.in === 'query').map((p) => p.name),
  );
  for (const name of new URLSearchParams(query).keys()) {
    if (!queryNames.has(name)) errors.push(`${template} takes no query parameter ${name}`);
  }
  const schema = operation.requestBody?.content['application/json']?.schema;
  if (body === undefined || body === null) {
    if (operation.requestBody?.required) errors.push(`${template} requires a JSON body`);
  } else if (!schema) {
    errors.push(`${method.toUpperCase()} ${template} takes no JSON body`);
  } else {
    errors.push(...typeErrors(JSON.parse(String(body)), schema, 'body'));
  }
  return errors;
}

/** Assert that every request the mocked `fetchWithAuth` was given agrees with the contract. */
export function expectRequestsMatchContract(): void {
  const calls = vi.mocked(fetchWithAuth).mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  const report = calls.flatMap(([, path, init]) => {
    const method = init?.method ?? 'GET';
    return contractErrors(method, path, init?.body).map((error) => `${method} ${path}: ${error}`);
  });
  expect(report).toEqual([]);
}
