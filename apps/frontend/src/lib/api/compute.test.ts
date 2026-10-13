import { afterEach, describe, expect, it, vi } from 'vitest';

import * as client from './client';
import {
  extendComputeAllocation,
  createAutomaticComputeSetup,
  getComputeAutomaticSetup,
  getCloudLabAutoscaling,
  getCloudLabPreferredNodes,
  getComputeOperations,
  importComputeResource,
  removeComputeServer,
  rotateComputeAllocation,
  saveComputeConnection,
  setComputePolicy,
  updateCloudLabAutoscaling,
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

describe('CloudLab auto-scaling API', () => {
  it('reads CloudLab auto-scaling status without caching it', async () => {
    const response = {
      policy: { revision: 3 },
      runtime: { generation: 9 },
      members: [],
      profile_reservations: [],
    };
    const fetch = stub(200, response);
    const controller = new AbortController();

    await expect(getCloudLabAutoscaling(controller.signal)).resolves.toEqual(response);

    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-autoscaling/cloudlab');
    expect(fetch.mock.calls[0][2]).toMatchObject({
      cache: 'no-store',
      signal: controller.signal,
    });
  });

  it('reads the live preferred-node shortlist without caching it', async () => {
    const response = {
      nodes: [
        {
          node_type: 'c6620',
          site: 'Utah',
          available: 54,
          total: 132,
          introduced_year: 2025,
          physical_cores: 28,
          logical_threads: 56,
          memory_gib: 128,
        },
      ],
      observed_at: '2026-10-11T12:00:00Z',
    };
    const fetch = stub(200, response);
    const controller = new AbortController();

    await expect(getCloudLabPreferredNodes(controller.signal)).resolves.toEqual(response);

    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-availability/cloudlab');
    expect(fetch.mock.calls[0][2]).toMatchObject({
      cache: 'no-store',
      signal: controller.signal,
    });
  });

  it('replaces the CloudLab auto-scaling policy at the revision read', async () => {
    const fetch = stub();
    const request = {
      expected_revision: 3,
      policy: {
        enabled: true,
        min_hosts: 1,
        max_hosts: 2,
        connection_id: 'cloudlab-account',
        ssh_connection_id: 'ssh-key',
        configuration: {
          project: 'FAST25',
          profile_project: 'CloudLab',
          profile_name: 'cloud-agent',
          duration: 8,
          bindings: { phystype: 'm510' },
          ssh_username: 'operator',
          host_key_source: 'registration' as const,
        },
        deployment_profiles: ['profile-a', 'profile-b'],
        sandbox_storage_path: '/mnt/cloud-agent',
        renewal_policy: policy,
        slot_policy: {
          formula_version: 1 as const,
          cpu_reservation_fraction: 0.75,
          cpu_per_slot: 2,
          memory_reserve_gib: 8,
          memory_per_slot_gib: 4,
          min_slots: 2,
          max_slots: 128,
        },
        scale_up_sustain_seconds: 120,
        scale_down_idle_seconds: 3600,
        cooldown_seconds: 600,
        scale_down_enabled: false,
      },
    };

    await updateCloudLabAutoscaling(request);

    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-autoscaling/cloudlab');
    expect(fetch.mock.calls[0][2]?.method).toBe('PUT');
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual(request);
    expect(JSON.stringify(fetch.mock.calls[0][2])).not.toContain('private-key');
  });
});

describe('compute API contracts', () => {
  it('permanently removes the encoded server at the reviewed revision', async () => {
    const fetch = stub(200, { removed: true });

    await expect(removeComputeServer('resource/one', 4)).resolves.toEqual({ removed: true });

    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/servers/resource%2Fone');
    expect(fetch.mock.calls[0][2]?.method).toBe('DELETE');
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual({ expected_revision: 4 });
  });

  it('checks automatic setup availability without caching deployment readiness', async () => {
    const readiness = { available: true, deployment_sha: 'c'.repeat(40), blocked_reasons: [] };
    const fetch = stub(200, readiness);
    const controller = new AbortController();
    await expect(getComputeAutomaticSetup(controller.signal)).resolves.toEqual(readiness);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-automatic-setup');
    expect(fetch.mock.calls[0][2]).toMatchObject({
      cache: 'no-store',
      signal: controller.signal,
    });
  });

  it('creates automatic setup using connection references and a stable intent', async () => {
    const fetch = stub();
    const request = {
      request_id: 'setup-once',
      gateway: {
        connection_id: 'gateway-key',
        address: 'gateway.example.org',
        port: 22,
        username: 'operator',
      },
      control: {
        connection_id: 'control-key',
        address: 'control.example.org',
        port: 22,
        username: 'operator',
      },
      gateway_port: 8080,
      chat_network_pool: '172.29.0.0/16',
      dns_server: '1.1.1.1',
      protected_cidrs: ['203.0.113.0/24'],
      slots: 2,
    };
    await createAutomaticComputeSetup(request);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/compute-deployment-profiles/automatic');
    expect(fetch.mock.calls[0][2]?.method).toBe('POST');
    expect(JSON.parse(String(fetch.mock.calls[0][2]?.body))).toEqual(request);
  });

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
