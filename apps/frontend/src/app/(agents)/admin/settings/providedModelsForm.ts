import {
  isModelId,
  sameBaseUrl,
  sameList,
  validBaseUrl,
} from '@/components/features/settings/modelConnectionChanges';
import {
  DEFAULT_USER_CONCURRENCY,
  MAX_DAILY_REQUESTS,
  MAX_GLOBAL_CONCURRENCY,
  MAX_USER_CONCURRENCY,
  OPERATOR_MAX_MODELS,
  OPERATOR_MAX_NAME_CHARS,
  type OperatorModelConnection,
  type OperatorModelConnectionCreate,
  type OperatorModelConnectionUpdate,
  type OperatorModelLimit,
} from '@/lib/api/adminOperatorModels';
import { invalidFields, refusedModels } from '@/lib/api/modelConnections';
import type { Translator } from '@/lib/i18n/translate';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

// What the provided-models forms hold, what they send, and how a refusal
// reads. Kept apart from the components so each rule has one place and a test.

/** What the connection form holds. Numbers stay as typed until they are sent. */
export interface ProvidedFields {
  name: string;
  base_url: string;
  /** Write-only; blank keeps the saved key on an edit. */
  key: string;
  models: string[];
  /** One of `models`, or `''` for none. */
  default_model: string;
  is_default: boolean;
  /** `''` sends no header. */
  end_user_header: string;
  user_concurrency: string;
}

/** A field the administrator can fix, as the form and the server name it. */
export type ProvidedField =
  | 'name'
  | 'base_url'
  | 'api_key'
  | 'models'
  | 'default_model'
  | 'end_user_header'
  | 'user_concurrency';

export const PROVIDED_FIELDS: readonly ProvidedField[] = [
  'name',
  'base_url',
  'api_key',
  'models',
  'default_model',
  'end_user_header',
  'user_concurrency',
];

/** An HTTP header name: a token of up to 64 characters. */
const HEADER_NAME = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/;

/** A whole number within `[minimum, maximum]` typed into a field, or `null`. */
export function wholeNumber(text: string, minimum: number, maximum: number): number | null {
  const clean = text.trim();
  if (!/^\d+$/.test(clean)) return null;
  const value = Number(clean);
  return value >= minimum && value <= maximum ? value : null;
}

export function initialFields(connection?: OperatorModelConnection): ProvidedFields {
  return {
    name: connection?.name ?? '',
    base_url: connection?.base_url ?? '',
    key: '',
    models: connection?.models ?? [],
    default_model: connection?.default_model ?? '',
    is_default: connection?.is_default ?? false,
    end_user_header: connection?.end_user_header ?? '',
    user_concurrency: String(connection?.user_concurrency ?? DEFAULT_USER_CONCURRENCY),
  };
}

/** What is wrong with each field before anything is sent; empty when nothing is. */
export function providedFieldProblems(
  fields: ProvidedFields,
  creating: boolean,
): Partial<Record<ProvidedField, true>> {
  const problems: Partial<Record<ProvidedField, true>> = {};
  if (!fields.name || fields.name.length > OPERATOR_MAX_NAME_CHARS) problems.name = true;
  if (!validBaseUrl(fields.base_url)) problems.base_url = true;
  if ((creating && !fields.key) || /[^\x21-\x7e]/.test(fields.key)) problems.api_key = true;
  const models = fields.models;
  if (!models.length || models.length > OPERATOR_MAX_MODELS || !models.every(isModelId)) {
    problems.models = true;
  }
  if (fields.default_model && !models.includes(fields.default_model)) {
    problems.default_model = true;
  }
  if (fields.end_user_header && !HEADER_NAME.test(fields.end_user_header)) {
    problems.end_user_header = true;
  }
  if (wholeNumber(fields.user_concurrency, 1, MAX_USER_CONCURRENCY) === null) {
    problems.user_concurrency = true;
  }
  return problems;
}

/** The body of a create. `is_default` is not part of it: it is set by an edit. */
export function newProvidedConnection(fields: ProvidedFields): OperatorModelConnectionCreate {
  return {
    name: fields.name,
    base_url: fields.base_url,
    api_key: fields.key,
    models: fields.models,
    default_model: fields.default_model || null,
    end_user_header: fields.end_user_header || null,
    user_concurrency:
      wholeNumber(fields.user_concurrency, 1, MAX_USER_CONCURRENCY) ?? DEFAULT_USER_CONCURRENCY,
  };
}

/**
 * Only what the administrator changed, so a rename never re-verifies a model
 * and a blank key keeps the stored one. `''` clears the default model and the
 * end-user header.
 */
export function providedChanges(
  connection: OperatorModelConnection,
  fields: ProvidedFields,
): OperatorModelConnectionUpdate {
  const changes: OperatorModelConnectionUpdate = {};
  if (fields.name !== connection.name) changes.name = fields.name;
  if (!sameBaseUrl(fields.base_url, connection.base_url)) changes.base_url = fields.base_url;
  if (fields.key) changes.api_key = fields.key;
  if (!sameList(fields.models, connection.models)) changes.models = fields.models;
  if (fields.default_model !== (connection.default_model ?? '')) {
    changes.default_model = fields.default_model;
  }
  if (fields.is_default !== connection.is_default) changes.is_default = fields.is_default;
  if (fields.end_user_header !== (connection.end_user_header ?? '')) {
    changes.end_user_header = fields.end_user_header;
  }
  const concurrency = wholeNumber(fields.user_concurrency, 1, MAX_USER_CONCURRENCY);
  if (concurrency !== null && concurrency !== connection.user_concurrency) {
    changes.user_concurrency = concurrency;
  }
  return changes;
}

/**
 * Whether saving calls the provider, which can take minutes: a new connection,
 * a new key or Base URL, or an added model. Anything else saves at once.
 */
export function providedNeedsVerification(
  connection: OperatorModelConnection | undefined,
  fields: ProvidedFields,
): boolean {
  if (!connection) return true;
  if (fields.key || !sameBaseUrl(fields.base_url, connection.base_url)) return true;
  return fields.models.some((model) => !connection.models.includes(model));
}

/** The listed models with no default allowance yet: the connection cannot be enabled. */
export function modelsWithoutLimit(connection: OperatorModelConnection): string[] {
  return connection.models.filter((model) => !Object.hasOwn(connection.limits ?? {}, model));
}

/**
 * How an allowance is chosen in a form. `limited` reads the number typed
 * beside it; `named` is `0`, which offers a model only to accounts given an
 * allowance of their own (or, for one account, withholds it); `unset` is a
 * model with no default yet, which cannot be saved.
 */
export type AllowanceMode = 'unset' | 'limited' | 'unlimited' | 'named';

export function allowanceMode(limit: OperatorModelLimit | undefined): AllowanceMode {
  if (!limit) return 'unset';
  if (limit.user_daily_requests === null) return 'unlimited';
  return limit.user_daily_requests === 0 ? 'named' : 'limited';
}

/** The daily allowance a form holds: a number, `null` for unlimited, `undefined` when invalid. */
export function allowanceValue(mode: AllowanceMode, text: string): number | null | undefined {
  if (mode === 'unlimited') return null;
  if (mode === 'named') return 0;
  if (mode === 'unset') return undefined;
  return wholeNumber(text, 1, MAX_DAILY_REQUESTS) ?? undefined;
}

/** An optional concurrency cap: `null` when blank, `undefined` when invalid. */
export function concurrencyValue(text: string): number | null | undefined {
  if (!text.trim()) return null;
  return wholeNumber(text, 1, MAX_GLOBAL_CONCURRENCY) ?? undefined;
}

/** The refusal of a create, a verifying edit or an enable while model connections are off. */
const FEATURE_OFF = 'model_connections_disabled';

/**
 * Whether a failure means this administrator's access ended. Not every 403
 * does: with model connections switched off for the deployment the server
 * refuses to add, verify or enable a connection with one, and the
 * administrator is still an administrator.
 */
export function isAccessDenied(cause: unknown): boolean {
  if (!(cause instanceof APIError)) return false;
  return cause.statusCode === 401 || (cause.statusCode === 403 && cause.code !== FEATURE_OFF);
}

const REFUSALS = {
  [FEATURE_OFF]: 'adminModels.error.featureOff',
  operator_model_connection_changed: 'connections.changed',
  model_connection_check_limit: 'connections.limited',
  model_connection_verification_timeout: 'connections.timedOut',
  operator_model_connection_name_taken: 'adminModels.error.nameTaken',
  operator_model_connection_not_found: 'adminModels.error.notFound',
  operator_model_limits_missing: 'adminModels.error.limitsMissing',
  account_not_found: 'adminModels.error.accountNotFound',
} as const;

/**
 * Why a request failed, in the administrator's words. Refusals this page
 * knows get its own sentence; anything else shows what the server said.
 */
export function providedFailure(cause: unknown, t: Translator): string {
  if (refusedModels(cause)) return t('connections.refused');
  if (invalidFields(cause)) return t('connections.invalid');
  if (cause instanceof APIError) {
    if (Object.hasOwn(REFUSALS, cause.code))
      return t(REFUSALS[cause.code as keyof typeof REFUSALS]);
    if (cause.statusCode === 504) return t('connections.timedOut');
    if (cause.statusCode === 429) return t('connections.limited');
  }
  return cause instanceof APIError ? getErrorMessage(cause) : t('adminModels.error.generic');
}

/** The fields a refused request named, as the form's own field names. */
export function refusedFields(cause: unknown): Partial<Record<ProvidedField, true>> {
  const problems: Partial<Record<ProvidedField, true>> = {};
  for (const field of invalidFields(cause) ?? []) {
    const name = field.split('.')[0] as ProvidedField;
    if (PROVIDED_FIELDS.includes(name)) problems[name] = true;
  }
  return problems;
}

/** A UTC day as `YYYY-MM-DD`, `daysAgo` days before `now`. */
export function utcDay(daysAgo = 0, now: number = Date.now()): string {
  return new Date(now - daysAgo * 86_400_000).toISOString().slice(0, 10);
}

/** A whole count, grouped for reading. */
export function countText(value: number): string {
  return value.toLocaleString('en-US');
}
