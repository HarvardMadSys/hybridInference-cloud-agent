import { afterEach, describe, expect, it, vi } from 'vitest';

import { APIError } from '@/lib/utils/errors';

import {
  clearOperatorModelUserLimit,
  createOperatorModelConnection,
  deleteOperatorModelConnection,
  getOperatorModelUsage,
  listOperatorModelConnections,
  listOperatorModelUsers,
  setOperatorModelLimit,
  setOperatorModelUserLimit,
  updateOperatorModelConnection,
} from './adminOperatorModels';
import * as client from './client';
import { invalidFields, refusedModels } from './modelConnections';

afterEach(() => vi.restoreAllMocks());

const ID = 'oconn_0123456789abcdef0123456789abcdef';
const ROOT = '/admin/agent/operator-model-connections';
const JSON_HEADERS = { 'Content-Type': 'application/json' };

/** Every request is answered with `body`; a response's body can be read only once. */
function answer(body: unknown, status = 200) {
  return vi
    .spyOn(client, 'fetchWithAuth')
    .mockImplementation(async () =>
      status === 204
        ? new Response(null, { status })
        : new Response(JSON.stringify(body), { status, headers: JSON_HEADERS }),
    );
}

describe('admin provided-model connections client', () => {
  it('lists connections without caching, and supports abort', async () => {
    const list = { connections: [], relay_hosts: 0, feature_enabled: true };
    const fetch = answer(list);
    const controller = new AbortController();
    await expect(listOperatorModelConnections(controller.signal)).resolves.toEqual(list);
    expect(fetch).toHaveBeenCalledWith(expect.any(String), ROOT, {
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('creates with the key in the body, and edits only what it is given', async () => {
    const fetch = answer({ id: ID }, 201);
    const input = {
      name: 'House models',
      base_url: 'https://api.example.com/v1',
      api_key: 'sk-test',
      models: ['house-fast'],
      default_model: 'house-fast',
      end_user_header: null,
      user_concurrency: 4,
    };
    await createOperatorModelConnection(input);
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), ROOT, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(input),
    });

    await updateOperatorModelConnection(ID, { enabled: true });
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), `${ROOT}/${ID}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ enabled: true }),
    });
  });

  it('deletes a connection, and reports a refusal', async () => {
    const fetch = answer(null, 204);
    await expect(deleteOperatorModelConnection(ID)).resolves.toBeUndefined();
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), `${ROOT}/${ID}`, {
      method: 'DELETE',
    });

    answer(
      { detail: { error: { type: 'operator_model_connection_not_found', message: 'Gone.' } } },
      404,
    );
    await expect(deleteOperatorModelConnection(ID)).rejects.toMatchObject({
      code: 'operator_model_connection_not_found',
      message: 'Gone.',
      statusCode: 404,
    });
  });

  it('sets a model’s default allowance, keeping null for unlimited', async () => {
    const fetch = answer({ id: ID });
    const limit = { model: 'house-fast', user_daily_requests: null, global_concurrency: 8 };
    await setOperatorModelLimit(ID, limit);
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), `${ROOT}/${ID}/limits`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(limit),
    });
    expect(JSON.parse(fetch.mock.calls[0][2]?.body as string).user_daily_requests).toBeNull();
  });

  it('pages through accounts by cursor', async () => {
    const fetch = answer({ users: [], next_cursor: null });
    await listOperatorModelUsers(ID);
    expect(fetch.mock.calls[0][1]).toBe(`${ROOT}/${ID}/users`);
    await listOperatorModelUsers(ID, { cursor: 'acct 9', limit: 50 });
    expect(fetch.mock.calls[1][1]).toBe(`${ROOT}/${ID}/users?cursor=acct+9&limit=50`);
    expect(fetch.mock.calls[1][2]).toMatchObject({ cache: 'no-store' });
  });

  it('sets an account’s own allowance by email or id, and clears it by account id and model', async () => {
    const row = { account_id: 'acct_1', email: 'a@example.com', limits: {}, used_today: {} };
    const fetch = answer(row);
    const limit = { account: 'a@example.com', model: 'house-fast', daily_requests: 0 };
    await expect(setOperatorModelUserLimit(ID, limit)).resolves.toEqual(row);
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), `${ROOT}/${ID}/user-limits`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(limit),
    });

    const cleared = answer(null, 204);
    await clearOperatorModelUserLimit(ID, 'acct 1', 'vendor/model:v2');
    expect(cleared).toHaveBeenLastCalledWith(
      expect.any(String),
      `${ROOT}/${ID}/user-limits?account_id=acct+1&model=vendor%2Fmodel%3Av2`,
      { method: 'DELETE' },
    );
  });

  it('reads usage between two UTC days', async () => {
    const usage = { rows: [], truncated: false };
    const fetch = answer(usage);
    await expect(
      getOperatorModelUsage(ID, { from: '2026-11-01', to: '2026-11-12' }),
    ).resolves.toEqual(usage);
    expect(fetch.mock.calls[0][1]).toBe(`${ROOT}/${ID}/usage?from=2026-11-01&to=2026-11-12`);
  });

  it('keeps each model’s result from a refused verification, as the owner’s client does', async () => {
    const models = [{ model: 'house-fast', status: 'failed', category: 'auth', runtimes: [] }];
    answer(
      {
        error: {
          type: 'model_connection_verification_failed',
          message: 'A model could not be verified.',
          models,
        },
      },
      422,
    );
    const cause = await createOperatorModelConnection({
      name: 'House models',
      base_url: 'https://api.example.com/v1',
      api_key: 'sk-test',
      models: ['house-fast'],
    }).catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(APIError);
    expect(refusedModels(cause)).toEqual(models);
    expect(invalidFields(cause)).toBeNull();
  });

  it('keeps which fields an invalid request named, never their values', async () => {
    answer(
      {
        error: {
          type: 'invalid_model_connection',
          message: 'Check the fields.',
          fields: [
            { field: 'base_url', message: 'Use https.' },
            { field: 'end_user_header', message: 'Not a header name.' },
          ],
        },
      },
      422,
    );
    const cause = await updateOperatorModelConnection(ID, { base_url: 'http://x' }).catch(
      (error: unknown) => error,
    );
    expect(invalidFields(cause)).toEqual(['base_url', 'end_user_header']);
    expect(refusedModels(cause)).toBeNull();
  });
});
