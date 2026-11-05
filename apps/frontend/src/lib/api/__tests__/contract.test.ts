import { describe, expect, it } from 'vitest';
import { contractErrors } from './contract';

const body = (value: unknown) => JSON.stringify(value);

describe('the contract check', () => {
  it('accepts a request the server accepts', () => {
    expect(
      contractErrors('PUT', '/v1/agent/memories/settings', body({ repo: null, enabled: true })),
    ).toEqual([]);
    expect(contractErrors('GET', '/v1/agent/memories?repo=acme%2Fapp')).toEqual([]);
  });

  it('prefers a literal segment over a parameter in the same place', () => {
    // `/memories/settings` is its own route, not `/memories/{memory_id}`,
    // which has no PUT.
    expect(
      contractErrors('PUT', '/v1/agent/memories/settings', body({ repo: null, enabled: false })),
    ).toEqual([]);
    expect(contractErrors('PUT', '/v1/agent/memories/amem_1', body({}))).toEqual([
      '/v1/agent/memories/{memory_id} has no PUT',
    ]);
  });

  it('names each disagreement', () => {
    expect(contractErrors('GET', '/v1/agent/memory')).toEqual([
      'no route in the contract matches /v1/agent/memory',
    ]);
    expect(contractErrors('GET', '/v1/agent/memories?scope=user')).toEqual([
      '/v1/agent/memories takes no query parameter scope',
    ]);
    expect(
      contractErrors('PATCH', '/v1/agent/memories/amem_1', body({ content: 'x', version: 3 })),
    ).toEqual([
      'body.repo is required',
      'body.expected_version is required',
      'body.version is not a field the server accepts',
    ]);
    expect(
      contractErrors('POST', '/v1/agent/memories/amem_1/restore', body({ expected_version: '3' })),
    ).toEqual(['body.expected_version: expected integer, got string']);
    expect(contractErrors('POST', '/v1/agent/memories')).toEqual([
      '/v1/agent/memories requires a JSON body',
    ]);
    expect(contractErrors('GET', '/v1/agent/memories/projects', body({}))).toEqual([
      'GET /v1/agent/memories/projects takes no JSON body',
    ]);
  });
});
