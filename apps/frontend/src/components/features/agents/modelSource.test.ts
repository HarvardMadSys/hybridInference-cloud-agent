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
  models: ['gpt-5', '[odd]'],
  model_connections: [
    { id: A, name: 'Alpha', models: ['gpt-5'], runtimes: ['codex', 'opencode'] },
    { id: B, name: 'Beta', models: ['gpt-5'], runtimes: ['opencode'] },
  ],
  github_connected: true,
  github_install_url: null,
} as AgentConfigApi;

describe('model choices', () => {
  it('gives every source of one model name its own identity', () => {
    const keys = offeredChoices(config, 'opencode').map(choiceKey);
    expect(new Set(keys).size).toBe(keys.length);
    // A Gateway name keeps its own spelling; one that looks like a key cannot pass for one.
    expect(keys.slice(0, 2)).toEqual(['gpt-5', '[null,"[odd]"]']);
  });

  it('filters connections by the runtime’s protocol', () => {
    expect(offeredChoices(config, 'claude-code')).toEqual([
      { model: 'gpt-5', connectionId: null },
      { model: '[odd]', connectionId: null },
    ]);
    expect(offeredChoices(config, 'codex').map((choice) => choice.connectionId)).toEqual([
      null,
      null,
      A,
    ]);
  });

  it('reads a legacy draft string as a Gateway model, never as an encoded key', () => {
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
  });

  it('offers nothing to pick when nothing can run, so the inherited model is named', () => {
    const empty = { ...config, models: [], model_connections: [] };
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

describe('translated sources', () => {
  const chatOnly = {
    ...config,
    models: [],
    model_connections: [
      {
        id: A,
        name: 'Chat only',
        models: ['m'],
        runtimes: ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
        translated_runtimes: ['claude-code', 'codex'],
      },
    ],
  } as AgentConfigApi;

  it('says when an agent reaches a connection through the compatibility helper', () => {
    const choice = { model: 'm', connectionId: A };
    expect(choiceLabel(chatOnly, choice, t, { runtime: 'codex' })).toBe(
      'm · Chat only · via Chat Completions',
    );
    expect(choiceLabel(chatOnly, choice, t, { runtime: 'opencode' })).toBe('m · Chat only');
    expect(choiceLabel(chatOnly, choice, t, { runtime: 'codex', unavailable: true })).toBe(
      'm · Chat only · via Chat Completions (unavailable)',
    );
  });

  it('offers a chat-only connection to Codex and Claude Code as well', () => {
    for (const runtime of ['codex', 'claude-code', 'pi']) {
      expect(offeredChoices(chatOnly, runtime)).toEqual([{ model: 'm', connectionId: A }]);
    }
  });
});
