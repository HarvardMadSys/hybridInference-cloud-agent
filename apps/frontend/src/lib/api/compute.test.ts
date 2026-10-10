import { afterEach, describe, expect, it, vi } from 'vitest';

import * as client from './client';
import {
  extendComputeAllocation,
  getComputeOperations,
  importComputeResource,
  rotateComputeAllocation,
  saveComputeConnection,
  setComputePolicy,
  type RenewalPolicy,
  type ServerRequest,
} from './compute';

const policy: RenewalPolicy = {
  enabled: true,
  renew_before_seconds: 3600,
  extend_to_horizon_seconds: 14400,
  stop_new_groups_before_seconds: 1800,
  stop_claims_before_seconds: 300,
  rotation_enabled: false,
  max_node_age_seconds: 604800,
  node_types: [],
  node_type_binding: 'phystype',
  reason: 'Finish evaluation',
};

function stub(status = 200, body: unknown = {}) {
  return vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

afterEach(() => vi.restoreAllMocks());

describe('compute API contracts', () => {
  it('requests replacement with a revision and stable intent ID', async () => {
    const fetch = stub();
    const intent = { request_id: 'move-once', expected_revision: 4 };
    await rotateComputeAllocation('resource/one', intent);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-allocations/resource%2Fone/rotate');
    expect(fetch.mock.calls[0][2]?.method).toBe('POST');
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual(intent);
  });

  it('writes secrets only in an authenticated connection body and omits provider on update', async () => {
    const fetch = stub();
    await saveComputeConnection(
      {
        name: 'Cloud account',
        provider_id: 'cloudlab',
        configuration: {},
        secret: 'secret-token',
        expected_revision: 3,
      },
      'connection/one',
    );
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-connections/connection%2Fone');
    expect(fetch.mock.calls[0][2]?.method).toBe('PATCH');
    const body = JSON.parse(String(fetch.mock.calls[0][2]?.body));
    expect(body).toEqual({
      name: 'Cloud account',
      configuration: {},
      secret: 'secret-token',
      expected_revision: 3,
    });
    expect(String(fetch.mock.calls[0][1])).not.toContain('secret-token');
  });

  it('sends a revisioned nested policy without causing extension', async () => {
    const fetch = stub();
    await setComputePolicy('resource-a', policy, 4);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-allocations/resource-a/policy');
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual({
      policy,
      expected_revision: 4,
    });
  });

  it('sends fixed absolute expiry so retries cannot keep adding time', async () => {
    const fetch = stub();
    const intent = {
      request_id: 'request-a',
      target_expires_at: '2026-10-05T08:00:00Z',
      reason: 'Finish evaluation',
    };
    await extendComputeAllocation('resource-a', intent);
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual(intent);
  });

  it('imports observation without any SSH deployment fields', async () => {
    const fetch = stub();
    const request: ServerRequest = {
      request_id: 'request-a',
      name: 'Imported lab',
      provider_id: 'cloudlab',
      mode: 'attach',
      connection_id: 'account-a',
      ssh_connection_id: 'ssh-a',
      deployment_profile: 'secure-kata',
      configuration: { experiment_id: 'experiment-a' },
    };
    await importComputeResource(request);
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual({
      request_id: 'request-a',
      name: 'Imported lab',
      provider_id: 'cloudlab',
      connection_id: 'account-a',
      configuration: { experiment_id: 'experiment-a' },
    });
  });

  it('reconciles by encoded request ID, never a credential', async () => {
    const fetch = stub();
    await getComputeOperations('request/one two');
    expect(fetch.mock.calls[0][1]).toBe(
      '/admin/agent/compute-operations?request_id=request%2Fone%20two',
    );
  });

  it('surfaces safe typed conflict reasons returned by the backend', async () => {
    stub(409, {
      detail: {
        code: 'revision_conflict',
        message: 'Resource changed; review the current revision.',
      },
    });
    await expect(setComputePolicy('resource-a', policy, 4)).rejects.toMatchObject({
      statusCode: 409,
      message: 'Resource changed; review the current revision.',
    });
  });
});
