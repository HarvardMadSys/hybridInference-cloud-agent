import { describe, expect, it } from 'vitest';

import type { AgentRunnerHost, HostSettingDefinition } from '@/lib/api/admin';

import {
  amountIn,
  factNote,
  formatSettingValue,
  globalRollout,
  inputUnits,
  parseSettingInput,
  rolloutText,
  settingFact,
  shorteningEffect,
  unitFor,
} from './settings';

const TIMEOUT: HostSettingDefinition = {
  key: 'agent_timeout_s',
  env: 'AGENT_TIMEOUT_S',
  label: 'Turn time limit',
  description: 'How long one agent turn may run.',
  unit: 'seconds',
  default: 3600,
  minimum: 60,
  maximum: 86400,
  scope: 'runner',
};

const IMPORT: HostSettingDefinition = {
  ...TIMEOUT,
  key: 'source_import_max_mib',
  env: 'AGENT_SOURCE_IMPORT_MAX_MIB',
  label: 'Repository size limit',
  unit: 'MiB',
  default: 1024,
  minimum: 1,
  maximum: 65536,
};

const SESSIONS: HostSettingDefinition = {
  ...TIMEOUT,
  key: 'terminal_max_sessions',
  unit: 'count',
  default: 64,
  minimum: 1,
  maximum: 4096,
  scope: 'sandbox_manager',
};

const CPU: HostSettingDefinition = {
  ...TIMEOUT,
  key: 'sandbox_cpu_millicores',
  env: 'AGENT_SANDBOX_CPU_MILLICORES',
  label: 'Container CPU',
  unit: 'millicores',
  default: 2000,
  minimum: 100,
  maximum: 256000,
};

function host(over: Partial<AgentRunnerHost> = {}): AgentRunnerHost {
  return {
    drain_fence_epoch: '00000000-0000-0000-0000-000000000001',
    host: 'runner-a',
    active: false,
    last_worker_id: null,
    first_seen_at: '2026-09-11T12:00:00Z',
    last_seen_at: '2026-09-11T12:00:00Z',
    seconds_since_seen: 1,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: ['host-settings-v1'],
    slots_total: 3,
    slots_used: 0,
    runner_version: null,
    sandbox_manager_url: null,
    last_heartbeat_at: null,
    sandbox_runtime: null,
    sandbox_image_digest: null,
    runtime_versions: null,
    runtime_versions_available: null,
    capacity_epoch: null,
    capacity: null,
    settings_desired: {},
    settings_applied: { agent_timeout_s: 3600 },
    settings_local: { agent_timeout_s: 3600 },
    ...over,
  };
}

describe('settingFact', () => {
  it('uses the server value over the global one, and the deployment default with neither', () => {
    expect(
      settingFact(
        TIMEOUT,
        { agent_timeout_s: 7200 },
        host({ settings_desired: { agent_timeout_s: 900 } }),
      ),
    ).toMatchObject({ admin: 900, source: 'server' });
    expect(settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host())).toMatchObject({
      admin: 7200,
      source: 'global',
    });
    expect(settingFact(TIMEOUT, {}, host())).toMatchObject({
      admin: null,
      source: 'own',
      own: 3600,
      state: 'applied',
    });
  });

  it('says applying until the server reports the value it should use', () => {
    const pending = settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host());
    expect(pending).toMatchObject({ inUse: 3600, state: 'applying' });
    expect(factNote(pending)).toBe('set for all servers · applying 2 h');

    const done = settingFact(
      TIMEOUT,
      { agent_timeout_s: 7200 },
      host({ settings_applied: { agent_timeout_s: 7200 } }),
    );
    expect(done.state).toBe('applied');
    expect(factNote(done)).toBe('set for all servers');
  });

  it('treats a cleared value as applying until the server uses the deployment default', () => {
    const fact = settingFact(TIMEOUT, {}, host({ settings_applied: { agent_timeout_s: 7200 } }));
    expect(fact.state).toBe('applying');
    expect(factNote(fact)).toBe('deployment default · applying 1 h');
  });

  it('ignores a different legacy environment value when reporting a reset', () => {
    const applied = settingFact(
      TIMEOUT,
      {},
      host({
        settings_local: { agent_timeout_s: 999 },
        settings_applied: { agent_timeout_s: 3600 },
      }),
    );
    expect(applied).toMatchObject({ admin: null, own: 999, state: 'applied' });
    expect(factNote(applied)).toBe('deployment default');
    const waiting = settingFact(
      TIMEOUT,
      {},
      host({
        settings_local: { agent_timeout_s: 999 },
        settings_applied: { agent_timeout_s: 999 },
      }),
    );
    expect(waiting.state).toBe('applying');
    expect(factNote(waiting)).toBe('deployment default · applying 1 h');
  });

  it('never claims a value for a server that has not reported, or cannot apply it', () => {
    expect(
      settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host({ settings_applied: null })).state,
    ).toBe('unreported');
    const old = settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host({ capabilities: [] }));
    expect(old.state).toBe('unsupported');
    expect(factNote(old)).toBe('Redeploy this server to manage it here');
  });
});

describe('globalRollout', () => {
  it('counts servers applying the global value apart from those overriding it', () => {
    const hosts = [
      host({ host: 'a', settings_applied: { agent_timeout_s: 7200 } }),
      host({ host: 'b' }),
      host({ host: 'c', settings_desired: { agent_timeout_s: 900 } }),
      host({ host: 'd', capabilities: [] }),
    ];
    expect(globalRollout(TIMEOUT, { agent_timeout_s: 7200 }, hosts)).toEqual({
      applied: 1,
      waiting: 1,
      overridden: 1,
      unsupported: 1,
    });
    expect(rolloutText(TIMEOUT, { agent_timeout_s: 7200 }, hosts)).toBe(
      'Applied on 1 of 2 servers · c sets its own (15 min) · d needs a redeploy',
    );
  });

  it('names the servers that differ by the name the page gives them, and counts many', () => {
    const own = (name: string) => host({ host: name, settings_desired: { agent_timeout_s: 900 } });
    const names: Record<string, string> = { a: 'rtx6000b' };
    expect(rolloutText(TIMEOUT, {}, [own('a')], (h) => names[h.host] ?? h.host)).toBe(
      'Deployment default: 1 h · rtx6000b sets its own (15 min)',
    );
    expect(rolloutText(TIMEOUT, {}, [own('a'), own('b'), own('c')])).toBe(
      'Deployment default: 1 h · 3 servers set their own',
    );
    const old = (name: string) => host({ host: name, capabilities: [] });
    expect(rolloutText(TIMEOUT, {}, [old('a'), old('b'), old('c')])).toBe(
      'Deployment default: 1 h · 3 servers need a redeploy',
    );
  });

  it('says each server uses its own when nothing is set globally', () => {
    expect(rolloutText(TIMEOUT, {}, [host()])).toBe('Deployment default: 1 h');
    expect(rolloutText(TIMEOUT, { agent_timeout_s: 3600 }, [host(), host({ host: 'b' })])).toBe(
      'In use on 2 servers',
    );
  });
});

describe('formatSettingValue', () => {
  it('uses the largest whole unit', () => {
    expect(formatSettingValue(TIMEOUT, 86400)).toBe('1 d');
    expect(formatSettingValue(TIMEOUT, 28800)).toBe('8 h');
    expect(formatSettingValue(TIMEOUT, 1800)).toBe('30 min');
    expect(formatSettingValue(TIMEOUT, 90)).toBe('90 s');
    expect(formatSettingValue(TIMEOUT, 15.5)).toBe('15.5 s');
    expect(formatSettingValue(IMPORT, 4096)).toBe('4 GiB');
    expect(formatSettingValue(IMPORT, 1500)).toBe('1500 MiB');
    expect(formatSettingValue(SESSIONS, 64)).toBe('64');
    expect(formatSettingValue(CPU, 2000)).toBe('2 vCPU');
    expect(formatSettingValue(CPU, 1250)).toBe('1.25 vCPU');
    expect(formatSettingValue(CPU, 101)).toBe('0.101 vCPU');
  });
});

describe('parseSettingInput', () => {
  it('accepts a whole number in bounds and explains anything else', () => {
    expect(parseSettingInput(TIMEOUT, ' 7200 ')).toEqual({ value: 7200 });
    for (const raw of ['', '59', '86401', '90.5', 'abc']) {
      expect(parseSettingInput(TIMEOUT, raw)).toEqual({
        error: 'Enter a value from 1 min to 1 d.',
      });
    }
    expect(parseSettingInput(SESSIONS, '0')).toEqual({
      error: 'Enter a whole number from 1 to 4096.',
    });
  });

  it('reads an amount in the unit it was typed in', () => {
    expect(parseSettingInput(TIMEOUT, '2', 'h')).toEqual({ value: 7200 });
    expect(parseSettingInput(TIMEOUT, '1.5', 'h')).toEqual({ value: 5400 });
    // 1.1 h is 3960 s; the float product is not, and must not be refused for it.
    expect(parseSettingInput(TIMEOUT, '1.1', 'h')).toEqual({ value: 3960 });
    expect(parseSettingInput(TIMEOUT, '90', 'min')).toEqual({ value: 5400 });
    expect(parseSettingInput(IMPORT, '10', 'GiB')).toEqual({ value: 10240 });
    expect(parseSettingInput(TIMEOUT, '25', 'h')).toEqual({
      error: 'Enter a value from 1 min to 1 d.',
    });
    expect(parseSettingInput(IMPORT, '0.3', 'GiB')).toEqual({
      error: 'Enter a value from 1 MiB to 64 GiB.',
    });
  });
});

describe('input units', () => {
  it('uses vCPU input while preserving whole millicores and enforcing CPU bounds', () => {
    expect(inputUnits(CPU)).toEqual(['vCPU']);
    expect(unitFor(CPU, 1250)).toBe('vCPU');
    expect(amountIn(1250, 'vCPU')).toBe('1.25');
    expect(parseSettingInput(CPU, '1.25', 'vCPU')).toEqual({ value: 1250 });
    expect(parseSettingInput(CPU, '0.101', 'vCPU')).toEqual({ value: 101 });
    for (const raw of ['0.099', '256.001', '0.1005']) {
      expect(parseSettingInput(CPU, raw, 'vCPU')).toEqual({
        error: 'Enter a value from 0.1 vCPU to 256 vCPU.',
      });
    }
  });

  it('reads and formats sandbox manager input limits in KiB', () => {
    const inputLimit: HostSettingDefinition = {
      ...SESSIONS,
      key: 'terminal_input_max_kib',
      unit: 'KiB',
      default: 64,
      minimum: 1,
      maximum: 64,
    };
    expect(inputUnits(inputLimit)).toEqual(['KiB']);
    expect(formatSettingValue(inputLimit, 32)).toBe('32 KiB');
    expect(parseSettingInput(inputLimit, '32', 'KiB')).toEqual({ value: 32 });
    expect(parseSettingInput(inputLimit, '65', 'KiB')).toEqual({
      error: 'Enter a value from 1 KiB to 64 KiB.',
    });
  });

  it('offers days only where the bound allows more than one', () => {
    expect(inputUnits(TIMEOUT)).toEqual(['s', 'min', 'h']);
    expect(inputUnits({ ...TIMEOUT, maximum: 604800 })).toEqual(['s', 'min', 'h', 'd']);
    expect(inputUnits(IMPORT)).toEqual(['MiB', 'GiB']);
    expect(inputUnits(SESSIONS)).toEqual([]);
  });

  it('opens a value in the largest unit it is a whole number of', () => {
    expect(unitFor(TIMEOUT, 28800)).toBe('h');
    expect(unitFor(TIMEOUT, 86400)).toBe('h');
    expect(unitFor(TIMEOUT, 1800)).toBe('min');
    expect(unitFor(TIMEOUT, 90)).toBe('s');
    expect(unitFor(IMPORT, 10240)).toBe('GiB');
    expect(unitFor(IMPORT, 1500)).toBe('MiB');
    expect(unitFor(SESSIONS, 64)).toBeNull();
    expect(amountIn(28800, 'h')).toBe('8');
    expect(amountIn(64, null)).toBe('64');
  });
});

describe('shorteningEffect', () => {
  it('warns only for the terminal limits that close open terminals', () => {
    const idle = { ...TIMEOUT, key: 'terminal_idle_ttl_s' };
    const lifetime = { ...TIMEOUT, key: 'terminal_hard_ttl_s' };
    expect(shorteningEffect(idle, 600)).toBe('Open terminals idle longer than 10 min will close.');
    expect(shorteningEffect(lifetime, 14400)).toBe('Open terminals older than 4 h will close.');
    expect(shorteningEffect(TIMEOUT, 600)).toBeNull();
  });
});
