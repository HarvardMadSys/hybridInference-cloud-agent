import { describe, expect, it } from 'vitest';
import type { AgentConfigApi } from '@/lib/api/agents';
import { composerOverrides } from './ConversationComposer';
import { effortFits, effortOffer, effortOverride, offeredEfforts } from './effort';

const CONNECTION = 'mconn_test';

function config(efforts?: Record<string, Record<string, unknown>>): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['claude-code', 'pi'],
    github_connected: true,
    github_install_url: null,
    model_connections: [
      {
        id: CONNECTION,
        name: 'Mine',
        models: ['deepseek-like', 'plain'],
        model_runtimes: { 'deepseek-like': ['claude-code', 'pi'], plain: ['claude-code'] },
        ...(efforts ? { model_efforts: efforts } : {}),
      },
    ],
  } as AgentConfigApi;
}

const declared = config({
  'deepseek-like': {
    'claude-code': { status: 'supported', levels: ['high', 'max'], reason: '' },
    pi: { status: 'unsupported', levels: [], reason: 'agent_cannot_express' },
  },
  plain: { 'claude-code': { status: 'unknown', levels: [], reason: 'not_declared' } },
});

describe('offered reasoning levels', () => {
  it('are the per-agent levels the server derived, not a blanket nothing', () => {
    // The regression: this answered [] for every model on every connection.
    expect(offeredEfforts(declared, 'claude-code', 'deepseek-like', CONNECTION)).toEqual([
      'high',
      'max',
    ]);
    expect(offeredEfforts(declared, 'pi', 'deepseek-like', CONNECTION)).toEqual([]);
    expect(effortOffer(declared, 'pi', 'deepseek-like', CONNECTION)?.status).toBe('unsupported');
    expect(effortOffer(declared, 'claude-code', 'plain', CONNECTION)?.status).toBe('unknown');
  });

  it('are nothing for a pair that cannot run, and unknown from an older server', () => {
    expect(effortOffer(declared, 'pi', 'plain', CONNECTION)).toBeNull();
    expect(effortOffer(declared, 'claude-code', 'deepseek-like', null)).toBeNull();
    expect(effortOffer(null, 'claude-code', 'deepseek-like', CONNECTION)).toBeNull();
    expect(effortOffer(config(), 'claude-code', 'deepseek-like', CONNECTION)).toMatchObject({
      status: 'unknown',
      levels: [],
    });
  });

  it('fit Auto always, and a level only where declared', () => {
    const offer = effortOffer(declared, 'claude-code', 'deepseek-like', CONNECTION);
    expect(effortFits(offer, '')).toBe(true);
    expect(effortFits(offer, 'max')).toBe(true);
    expect(effortFits(offer, 'low')).toBe(false);
    expect(effortFits(effortOffer(declared, 'claude-code', 'plain', CONNECTION), 'high')).toBe(
      false,
    );
  });
});

describe('a follow-up level against the turn it inherits from', () => {
  it('sends nothing when unchanged, the level when changed, "auto" to clear one', () => {
    expect(effortOverride('high', 'high')).toEqual({});
    expect(effortOverride(undefined, '')).toEqual({});
    expect(effortOverride(undefined, 'max')).toEqual({ reasoning_effort: 'max' });
    expect(effortOverride('high', 'max')).toEqual({ reasoning_effort: 'max' });
    expect(effortOverride('high', '')).toEqual({ reasoning_effort: 'auto' });
  });

  it('travels with the model override', () => {
    const parent = {
      model: 'deepseek-like',
      modelConnectionId: CONNECTION,
      reasoningEffort: 'max',
    };
    const same = { model: 'deepseek-like', connectionId: CONNECTION };
    expect(composerOverrides(parent, same, 'max')).toEqual({});
    expect(composerOverrides(parent, same, '')).toEqual({ reasoning_effort: 'auto' });
    expect(composerOverrides(parent, { model: 'plain', connectionId: CONNECTION }, '')).toEqual({
      model: 'plain',
      model_connection_id: CONNECTION,
      reasoning_effort: 'auto',
    });
  });
});
