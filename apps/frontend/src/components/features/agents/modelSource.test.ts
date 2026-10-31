import { describe, expect, it } from 'vitest';
import type { AgentConfigApi } from '@/lib/api/agents';
import { translate } from '@/lib/i18n/translate';
import {
  choiceKey,
  choiceLabel,
  modelOverride,
  modelPicker,
  offeredChoices,
  storedChoice,
  turnModelPicker,
} from './modelSource';

const A = 'mconn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const B = 'mconn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const t = (key: Parameters<typeof translate>[1], vars?: Record<string, string | number>) =>
  translate('en', key, vars);

const config = {
  repos: [],
  runtimes: ['claude-code', 'codex', 'opencode'],
  model_connections: [
    { id: A, name: 'Alpha', models: ['gpt-5'], model_runtimes: { 'gpt-5': ['codex', 'opencode'] } },
    { id: B, name: 'Beta', models: ['gpt-5'], model_runtimes: { 'gpt-5': ['opencode'] } },
  ],
  github_connected: true,
  github_install_url: null,
} as AgentConfigApi;

describe('model choices', () => {
  it('gives every source of one model name its own identity', () => {
    const keys = offeredChoices(config, 'opencode').map(choiceKey);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toEqual([`["${A}","gpt-5"]`, `["${B}","gpt-5"]`]);
    // A name that looks like a key cannot pass for one.
    expect(choiceKey({ model: '[odd]', connectionId: null })).toBe('[null,"[odd]"]');
  });

  it('keys and labels a model by its name alone where only one source offers it', () => {
    const picker = modelPicker(
      config,
      offeredChoices(config, 'codex'),
      {
        model: 'gpt-5',
        connectionId: A,
      },
      () => {},
      t,
    );
    expect(picker.options).toEqual(['gpt-5']);
    expect(picker.value).toBe('gpt-5');
    // Two connections list it, so the label still says which one this is.
    expect(picker.optionLabels['gpt-5']).toBe('gpt-5 · Alpha');
  });

  it('filters connections by the runtime’s protocol', () => {
    expect(offeredChoices(config, 'claude-code')).toEqual([]);
    expect(offeredChoices(config, 'codex').map((choice) => choice.connectionId)).toEqual([A]);
  });

  it('reads a legacy draft string as a model with no source, never as an encoded key', () => {
    expect(storedChoice(`["${A}","gpt-5"]`, undefined)).toEqual({
      model: `["${A}","gpt-5"]`,
      connectionId: null,
    });
    expect(storedChoice('gpt-5', '')).toEqual({ model: 'gpt-5', connectionId: null });
    expect(storedChoice('', A)).toEqual({ model: '', connectionId: null });
  });

  it('labels and keeps an unavailable personal choice instead of swapping it', () => {
    const gone = { model: 'gpt-5', connectionId: 'mconn_cccccccccccccccccccccccccccccccc' };
    const picker = modelPicker(config, offeredChoices(config, 'codex'), gone, () => {}, t);
    expect(picker.value).toBe(choiceKey(gone));
    expect(picker.options[0]).toBe(picker.value);
    expect(picker.optionLabels[picker.value]).toBe('gpt-5 · personal connection (unavailable)');
    expect(picker.optionLabels[choiceKey({ model: 'gpt-5', connectionId: A })]).toBe(
      'gpt-5 · Alpha',
    );
    expect(picker.options).toHaveLength(2);
  });

  it('offers nothing to pick when nothing can run, so the inherited model is named', () => {
    const empty = { ...config, model_connections: [] };
    const inherited = { model: 'gpt-5', connectionId: A };
    expect(turnModelPicker(empty, 'codex', inherited, () => {}, t, inherited)).toBeNull();
  });
});

describe('modelOverride', () => {
  const parent = { model: 'gpt-5', modelConnectionId: A };

  it('sends nothing for the inherited pair', () => {
    expect(modelOverride(parent, { model: 'gpt-5', connectionId: A })).toEqual({});
  });

  it('sends the model again when only the source changes', () => {
    expect(modelOverride(parent, { model: 'gpt-5', connectionId: B })).toEqual({
      model: 'gpt-5',
      model_connection_id: B,
    });
    expect(modelOverride(parent, { model: 'gpt-5', connectionId: null })).toEqual({
      model: 'gpt-5',
    });
    expect(
      modelOverride(
        { model: 'gpt-5', modelConnectionId: null },
        { model: 'gpt-5', connectionId: A },
      ),
    ).toEqual({ model: 'gpt-5', model_connection_id: A });
  });
});

describe('per-model availability', () => {
  const mixed = {
    ...config,
    model_connections: [
      {
        id: A,
        name: 'Mixed',
        models: ['verified', 'fresh'],
        // `fresh` was added after verification: it is listed, but not usable yet.
        model_runtimes: { verified: ['claude-code', 'codex', 'kilo', 'opencode', 'pi'] },
      },
    ],
  } as AgentConfigApi;

  it('offers only the models verified for the agent, never the whole connection', () => {
    for (const runtime of ['codex', 'claude-code', 'pi']) {
      expect(offeredChoices(mixed, runtime)).toEqual([{ model: 'verified', connectionId: A }]);
    }
    expect(offeredChoices(mixed, 'goose')).toEqual([]);
  });

  it('never names the API an agent reaches a model on', () => {
    const choice = { model: 'verified', connectionId: A };
    expect(choiceLabel(mixed, choice, t, { runtime: 'codex' })).toBe('verified');
    expect(
      choiceLabel(mixed, { model: 'fresh', connectionId: A }, t, {
        runtime: 'codex',
        unavailable: true,
      }),
    ).toBe('fresh (unavailable)');
  });
});
