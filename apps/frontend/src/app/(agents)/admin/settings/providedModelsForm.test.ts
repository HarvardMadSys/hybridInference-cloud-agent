import { describe, expect, it } from 'vitest';

import { translate } from '@/lib/i18n/translate';
import { OPERATOR_MAX_MODELS } from '@/lib/api/adminOperatorModels';
import { APIError } from '@/lib/utils/errors';

import {
  allowanceMode,
  allowanceValue,
  concurrencyValue,
  initialFields,
  isAccessDenied,
  modelsWithoutLimit,
  newProvidedConnection,
  providedChanges,
  providedFailure,
  providedFieldProblems,
  providedNeedsVerification,
  refusedFields,
  utcDay,
  wholeNumber,
  type ProvidedFields,
} from './providedModelsForm';
import { daily, providedConnection, refusal } from './providedModelsTestFixtures';

const t = (key: Parameters<typeof translate>[1], vars?: Record<string, string | number>) =>
  translate('en', key, vars);

const filled: ProvidedFields = {
  name: 'House models',
  base_url: 'https://api.example.com/v1',
  key: 'sk-test',
  models: ['house-fast'],
  default_model: 'house-fast',
  is_default: false,
  end_user_header: 'X-End-User',
  user_concurrency: '4',
};

describe('the provided connection form', () => {
  it('accepts a complete form, and names each field that is wrong', () => {
    expect(providedFieldProblems(filled, true)).toEqual({});
    expect(
      providedFieldProblems(
        {
          ...filled,
          name: '',
          base_url: 'http://api.example.com/v1',
          key: 'has space',
          models: ['bad id'],
          default_model: 'elsewhere',
          end_user_header: 'X End: User',
          user_concurrency: '65',
        },
        true,
      ),
    ).toEqual({
      name: true,
      base_url: true,
      api_key: true,
      models: true,
      default_model: true,
      end_user_header: true,
      user_concurrency: true,
    });
  });

  it('needs a key to create, and keeps the saved one on an edit', () => {
    expect(providedFieldProblems({ ...filled, key: '' }, true)).toEqual({ api_key: true });
    expect(providedFieldProblems({ ...filled, key: '' }, false)).toEqual({});
  });

  it('allows a name of 64 characters and as many models as the server does, and no more', () => {
    const ids = (count: number) => Array.from({ length: count }, (_, index) => `model-${index}`);
    expect(providedFieldProblems({ ...filled, name: 'n'.repeat(64) }, true)).toEqual({});
    expect(providedFieldProblems({ ...filled, name: 'n'.repeat(65) }, true)).toEqual({
      name: true,
    });
    const many = { ...filled, default_model: '' };
    expect(providedFieldProblems({ ...many, models: ids(OPERATOR_MAX_MODELS) }, true)).toEqual({});
    expect(providedFieldProblems({ ...many, models: ids(OPERATOR_MAX_MODELS + 1) }, true)).toEqual({
      models: true,
    });
  });

  it('creates with what was typed, sending null for what was left out', () => {
    expect(newProvidedConnection(filled)).toEqual({
      name: 'House models',
      base_url: 'https://api.example.com/v1',
      api_key: 'sk-test',
      models: ['house-fast'],
      default_model: 'house-fast',
      end_user_header: 'X-End-User',
      user_concurrency: 4,
    });
    expect(
      newProvidedConnection({ ...filled, default_model: '', end_user_header: '' }),
    ).toMatchObject({ default_model: null, end_user_header: null });
    // Where new users start is an edit, never part of the create.
    expect(newProvidedConnection({ ...filled, is_default: true })).not.toHaveProperty('is_default');
  });

  it('sends only what an edit changed', () => {
    const saved = providedConnection({
      default_model: 'house-fast',
      end_user_header: 'X-End-User',
    });
    const unchanged = initialFields(saved);
    expect(providedChanges(saved, unchanged)).toEqual({});
    expect(providedChanges(saved, { ...unchanged, name: 'Renamed' })).toEqual({ name: 'Renamed' });
    // The same address typed with a trailing slash is not a change.
    expect(
      providedChanges(saved, { ...unchanged, base_url: 'https://api.example.com/v1/' }),
    ).toEqual({});
    expect(
      providedChanges(saved, {
        ...unchanged,
        key: 'sk-new',
        models: ['house-fast'],
        is_default: true,
        user_concurrency: '8',
      }),
    ).toEqual({
      api_key: 'sk-new',
      models: ['house-fast'],
      is_default: true,
      user_concurrency: 8,
    });
  });

  it('clears the default model and the end-user header with an empty string', () => {
    const saved = providedConnection({
      default_model: 'house-fast',
      end_user_header: 'X-End-User',
    });
    expect(
      providedChanges(saved, { ...initialFields(saved), default_model: '', end_user_header: '' }),
    ).toEqual({ default_model: '', end_user_header: '' });
  });

  it('verifies with the provider only for a new key, a new Base URL or an added model', () => {
    const saved = providedConnection();
    const fields = initialFields(saved);
    expect(providedNeedsVerification(undefined, filled)).toBe(true);
    expect(providedNeedsVerification(saved, fields)).toBe(false);
    expect(providedNeedsVerification(saved, { ...fields, name: 'Renamed' })).toBe(false);
    expect(providedNeedsVerification(saved, { ...fields, models: ['house-fast'] })).toBe(false);
    expect(providedNeedsVerification(saved, { ...fields, key: 'sk-new' })).toBe(true);
    expect(
      providedNeedsVerification(saved, { ...fields, base_url: 'https://other.example.com/v1' }),
    ).toBe(true);
    expect(
      providedNeedsVerification(saved, { ...fields, models: [...saved.models, 'house-new'] }),
    ).toBe(true);
  });
});

describe('allowances in a form', () => {
  it('lists the models that still have no default allowance', () => {
    const none = providedConnection();
    expect(modelsWithoutLimit(none)).toEqual(['house-fast', 'house-deep']);
    // Unlimited and zero are both choices somebody made.
    const set = providedConnection({
      limits: { 'house-fast': daily(null), 'house-deep': daily(0) },
    });
    expect(modelsWithoutLimit(set)).toEqual([]);
  });

  it('reads a saved allowance as a number, unlimited, only named accounts, or not set', () => {
    expect(allowanceMode(undefined)).toBe('unset');
    expect(allowanceMode(daily(1500))).toBe('limited');
    expect(allowanceMode(daily(null))).toBe('unlimited');
    expect(allowanceMode(daily(0))).toBe('named');
  });

  it('turns a choice into the value sent: a number, null for unlimited, 0 for named', () => {
    expect(allowanceValue('limited', ' 1500 ')).toBe(1500);
    expect(allowanceValue('unlimited', 'ignored')).toBeNull();
    expect(allowanceValue('named', '')).toBe(0);
    // Nothing chosen, or a number that is not one: there is nothing to send.
    expect(allowanceValue('unset', '5')).toBeUndefined();
    for (const text of ['', '0', '-3', '1.5', 'many', '10000001']) {
      expect(allowanceValue('limited', text)).toBeUndefined();
    }
  });

  it('reads an optional concurrency cap: blank is none', () => {
    expect(concurrencyValue('')).toBeNull();
    expect(concurrencyValue('8')).toBe(8);
    expect(concurrencyValue('0')).toBeUndefined();
    expect(concurrencyValue('1025')).toBeUndefined();
    expect(wholeNumber('64', 1, 64)).toBe(64);
    expect(wholeNumber('65', 1, 64)).toBeNull();
  });
});

describe('how a refusal reads', () => {
  it('has a sentence for each refusal the page knows', () => {
    const said = (code: string, status: number) => providedFailure(refusal(code, 'raw', status), t);
    expect(said('operator_model_connection_name_taken', 409)).toBe(
      'Another provided connection already has this name.',
    );
    expect(said('operator_model_limits_missing', 409)).toMatch(
      /every model needs a default allowance first/,
    );
    expect(said('operator_model_connection_not_found', 404)).toMatch(/no longer exists/);
    expect(said('account_not_found', 404)).toBe('No account has that email address or id.');
    expect(said('model_connections_disabled', 403)).toMatch(
      /Model connections are switched off on this deployment/,
    );
    expect(said('operator_model_connection_changed', 409)).toMatch(
      /changed while this was being saved/,
    );
    expect(said('model_connection_check_limit', 429)).toMatch(/Too many verifications/);
    expect(said('model_connection_verification_timeout', 504)).toMatch(/took too long/);
    // A gateway's own timeout page carries no type: the status says enough.
    expect(said('TIMEOUT_ERROR', 504)).toMatch(/took too long/);
  });

  it('says a refused verification or an invalid field in its own words', () => {
    expect(
      providedFailure(
        refusal('model_connection_verification_failed', 'raw', 422, { models: [] }),
        t,
      ),
    ).toMatch(/a model could not be verified/);
    const invalid = refusal('invalid_model_connection', 'raw', 422, {
      fields: ['base_url', 'models.0', 'unknown_field'],
    });
    expect(providedFailure(invalid, t)).toBe('Check the highlighted fields, then try again.');
    expect(refusedFields(invalid)).toEqual({ base_url: true, models: true });
  });

  it('shows what the server said for a refusal it has no sentence for', () => {
    expect(providedFailure(refusal('provided_unavailable', 'The relay is down.', 503), t)).toBe(
      'The relay is down.',
    );
    expect(providedFailure(new Error('boom'), t)).toBe('The request failed. Try again.');
  });

  it('knows an ended session from any other failure', () => {
    expect(isAccessDenied(new APIError('forbidden', 'No.', 403))).toBe(true);
    expect(isAccessDenied(new APIError('unauthorized', 'No.', 401))).toBe(true);
    expect(isAccessDenied(new APIError('account_not_found', 'No.', 404))).toBe(false);
    // Model connections switched off is refused with a 403 too: the administrator still is one.
    expect(isAccessDenied(new APIError('model_connections_disabled', 'Off.', 403))).toBe(false);
    expect(isAccessDenied(new Error('offline'))).toBe(false);
  });
});

describe('UTC days', () => {
  it('counts back from today in UTC', () => {
    const now = Date.parse('2026-11-12T23:30:00-05:00');
    expect(utcDay(0, now)).toBe('2026-11-13');
    expect(utcDay(6, now)).toBe('2026-11-07');
  });
});
