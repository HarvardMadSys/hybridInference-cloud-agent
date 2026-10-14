import { afterEach, describe, expect, it, vi } from 'vitest';
import { saveEnvironment, saveGlobalVariables, startEnvironmentAction } from './environments';
import { variableEdits } from '@/components/features/agents/environment/VariableEditor';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());
const variables = [
  { name: 'TOKEN', secret: true, phase: 'both' as const, is_set: true, value: null },
  { name: 'REGION', secret: false, phase: 'runtime' as const, value: 'east' },
];
describe('environment credentials and commands', () => {
  it('keeps saved Secrets write-only while preserving ordinary display values', () => {
    expect(variableEdits(variables)).toEqual([
      {
        name: 'TOKEN',
        original_name: 'TOKEN',
        original_secret: true,
        secret: true,
        phase: 'both',
        operation: 'keep',
      },
      {
        name: 'REGION',
        original_name: 'REGION',
        original_secret: false,
        secret: false,
        phase: 'runtime',
        value: 'east',
        original_value: 'east',
        operation: 'keep',
      },
    ]);
  });
  it('sends only explicit edit fields, without resending kept values or display metadata', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
    await saveEnvironment({
      id: 'env-1',
      repo: 'org/repo',
      name: 'Development',
      revision: 3,
      setup_script: 'npm ci',
      start_script: '',
      verify_script: 'npm test',
      actions: [],
      variables: variableEdits(variables),
    });
    const body = JSON.parse(fetch.mock.calls[0][2]?.body as string);
    expect(body.variables).toEqual([
      { name: 'TOKEN', secret: true, phase: 'both', operation: 'keep' },
      { name: 'REGION', secret: false, phase: 'runtime', operation: 'keep' },
    ]);
    expect(fetch.mock.calls[0][2]?.cache).toBe('no-store');
    fetch.mockResolvedValue(
      new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    );
    await saveGlobalVariables(1, [
      { name: 'TOKEN', secret: true, phase: 'both', operation: 'replace', value: 'replacement' },
    ]);
    expect(JSON.parse(fetch.mock.lastCall?.[2]?.body as string).variables[0].value).toBe(
      'replacement',
    );
  });
  it('sends no edit for a row removed before it was ever saved', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
    await saveGlobalVariables(1, [
      ...variableEdits(variables),
      { name: '', secret: true, phase: 'both', operation: 'remove' },
    ]);
    expect(JSON.parse(fetch.mock.lastCall?.[2]?.body as string).variables).toEqual([
      { name: 'TOKEN', secret: true, phase: 'both', operation: 'keep' },
      { name: 'REGION', secret: false, phase: 'runtime', operation: 'keep' },
    ]);
  });
  it('sends a saved name removed and added again as the new row alone', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockImplementation(
        async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
      );
    const [token] = variableEdits(variables);
    await saveGlobalVariables(1, [
      { ...token, operation: 'remove', value: undefined },
      { name: 'TOKEN', secret: true, phase: 'both', operation: 'replace', value: 'replacement' },
    ]);
    expect(JSON.parse(fetch.mock.lastCall?.[2]?.body as string).variables).toEqual([
      { name: 'TOKEN', secret: true, phase: 'both', operation: 'replace', value: 'replacement' },
    ]);
    await saveGlobalVariables(1, [{ ...token, operation: 'remove', value: undefined }]);
    expect(JSON.parse(fetch.mock.lastCall?.[2]?.body as string).variables).toEqual([
      { name: 'TOKEN', secret: true, phase: 'both', operation: 'remove' },
    ]);
  });
  it('requests saved actions by identity and binding without sending command text', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
    await startEnvironmentAction('job', 'build', 'request-key');
    expect(fetch.mock.calls[0][1]).toBe('/v1/agent/jobs/job/thread/environment/actions/build/runs');
    expect(fetch.mock.calls[0][2]?.body).toBeUndefined();
    expect(fetch.mock.calls[0][2]?.headers).toHaveProperty('Idempotency-Key');
  });
});
