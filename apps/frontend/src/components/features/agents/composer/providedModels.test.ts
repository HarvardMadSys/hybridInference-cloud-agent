import { afterEach, describe, expect, it } from 'vitest';
import type { AgentConfigApi } from '@/lib/api/agents';
import { providedSource, TEST_PROVIDED, TEST_RESETS_AT } from '@/test/connectionConfig';
import {
  allowanceOf,
  findSource,
  hasRunnableProvidedModel,
  isProvidedId,
  leadingProvidedSource,
  modelSetupRequired,
  providedAllowance,
  providedSources,
  resetTime,
  usageSourceKey,
} from './providedModels';

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['codex'],
    github_connected: true,
    github_install_url: null,
    model_connections_enabled: true,
    ...overrides,
  };
}

describe('an allowance on a provided model', () => {
  const source = providedSource({
    limited: [100, 40],
    spent: [10, 10],
    over: [10, 12],
    open: [null, 5000],
  });

  it('is what is left of today’s requests, never below zero', () => {
    expect(allowanceOf(source, 'limited')).toEqual({
      limit: 100,
      used: 40,
      remaining: 60,
      exhausted: false,
      resetsAt: TEST_RESETS_AT,
    });
    expect(allowanceOf(source, 'spent')).toMatchObject({ remaining: 0, exhausted: true });
    // A count can pass its allowance when the allowance was lowered during the day.
    expect(allowanceOf(source, 'over')).toMatchObject({ remaining: 0, exhausted: true });
  });

  it('has nothing to run out of when unlimited', () => {
    expect(allowanceOf(source, 'open')).toMatchObject({
      limit: null,
      used: 5000,
      remaining: null,
      exhausted: false,
    });
  });

  it('starts over once the reset time has passed, without a new answer from the server', () => {
    const reset = Date.parse('2026-11-13T00:00:00Z');
    const yesterday = { ...source, resets_at: '2026-11-13T00:00:00Z' };
    expect(allowanceOf(yesterday, 'spent', reset - 1)).toMatchObject({ exhausted: true });
    expect(allowanceOf(yesterday, 'spent', reset)).toMatchObject({
      used: 0,
      remaining: 10,
      exhausted: false,
    });
  });

  it('does not limit a listed model the server sent no allowance for', () => {
    const bare = { ...source, models: ['limited', 'unlisted'] };
    expect(allowanceOf(bare, 'unlisted')).toMatchObject({ limit: null, exhausted: false });
  });

  it('is read by source id, and only for a model that source lists', () => {
    const loaded = config({ provided_sources: [source] });
    expect(providedAllowance(loaded, TEST_PROVIDED, 'limited')?.remaining).toBe(60);
    expect(providedAllowance(loaded, TEST_PROVIDED, 'elsewhere')).toBeNull();
    expect(providedAllowance(loaded, 'mconn_test', 'limited')).toBeNull();
    expect(providedAllowance(null, TEST_PROVIDED, 'limited')).toBeNull();
  });
});

describe('provided sources in the configuration', () => {
  const source = providedSource({ 'house-fast': [100, 0] });

  it('are found by id beside the owner’s own connections', () => {
    const loaded = config({
      provided_sources: [source],
      model_connections: [{ id: 'mconn_test', name: 'Mine', models: [], model_runtimes: {} }],
    });
    expect(findSource(loaded, TEST_PROVIDED)?.name).toBe('House models');
    expect(findSource(loaded, 'mconn_test')?.name).toBe('Mine');
    expect(findSource(loaded, 'oconn_ffffffffffffffffffffffffffffffff')).toBeUndefined();
    expect(findSource(loaded, null)).toBeUndefined();
    expect(isProvidedId(TEST_PROVIDED)).toBe(true);
    expect(isProvidedId('mconn_test')).toBe(false);
    expect(isProvidedId(null)).toBe(false);
  });

  it('are not offered where model connections are switched off, or before the feature', () => {
    expect(providedSources(config({ provided_sources: [source] }))).toEqual([source]);
    expect(
      providedSources(config({ provided_sources: [source], model_connections_enabled: false })),
    ).toEqual([]);
    expect(providedSources(config())).toEqual([]);
    expect(providedSources(null)).toEqual([]);
  });

  it('lead with the default source, else the first', () => {
    const other = { ...source, id: 'oconn_ffffffffffffffffffffffffffffffff', is_default: false };
    expect(leadingProvidedSource(config({ provided_sources: [other, source] }))).toBe(source);
    expect(leadingProvidedSource(config({ provided_sources: [other] }))).toBe(other);
    expect(leadingProvidedSource(config())).toBeUndefined();
  });
});

describe('whether the owner must add a connection of their own', () => {
  const runnable = providedSource({ 'house-fast': [100, 0] });

  it('follows the server', () => {
    expect(modelSetupRequired(config({ model_connection_required: true }))).toBe(true);
    expect(modelSetupRequired(config({ model_connection_required: false }))).toBe(false);
    expect(modelSetupRequired(config())).toBe(false);
    expect(modelSetupRequired(null)).toBe(false);
  });

  it('is never asked of an owner a provided model can run for', () => {
    const loaded = config({ model_connection_required: true, provided_sources: [runnable] });
    expect(hasRunnableProvidedModel(loaded)).toBe(true);
    expect(modelSetupRequired(loaded)).toBe(false);
  });

  it('is still asked where the provided source offers this owner nothing that runs', () => {
    // Every model withheld from this account: the source is listed with none.
    const withheld = providedSource({});
    // Listed, but no agent can run it yet.
    const unverified = providedSource({ 'house-fast': [100, 0] }, {}, []);
    for (const source of [withheld, unverified]) {
      const loaded = config({ model_connection_required: true, provided_sources: [source] });
      expect(hasRunnableProvidedModel(loaded)).toBe(false);
      expect(modelSetupRequired(loaded)).toBe(true);
    }
  });
});

describe('the line that says whose a turn’s usage is', () => {
  it('names this deployment for a provided turn, the owner’s provider otherwise', () => {
    expect(usageSourceKey({ source: 'provided' })).toBe('modelSource.usageProvided');
    expect(usageSourceKey({ source: 'personal' })).toBe('modelSource.usageExternal');
    // A control plane that predates provided models names no source.
    expect(usageSourceKey({})).toBe('modelSource.usageExternal');
    expect(usageSourceKey({ source: null })).toBe('modelSource.usageExternal');
    expect(usageSourceKey(null)).toBe('modelSource.usageExternal');
  });
});

describe('the reset time', () => {
  const zone = process.env.TZ;
  afterEach(() => {
    if (zone === undefined) delete process.env.TZ;
    else process.env.TZ = zone;
  });

  it('is midnight UTC in the viewer’s own time zone', () => {
    process.env.TZ = 'Asia/Shanghai';
    expect(resetTime('2026-11-13T00:00:00Z')).toMatch(/8:00/);
    process.env.TZ = 'America/New_York';
    expect(resetTime('2026-11-13T00:00:00Z')).toMatch(/7:00/);
  });

  it('is empty for a time that cannot be read', () => {
    expect(resetTime('soon')).toBe('');
  });
});
