'use client';

import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';

import { PasswordInput } from '@/components/features/auth/PasswordInput';
import { ModelIdsField } from '@/components/features/settings/ModelIdsField';
import { withModel } from '@/components/features/settings/modelConnectionChanges';
import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import {
  createOperatorModelConnection,
  MAX_USER_CONCURRENCY,
  OPERATOR_MAX_MODELS,
  OPERATOR_MAX_NAME_CHARS,
  updateOperatorModelConnection,
  type OperatorModelConnection,
} from '@/lib/api/adminOperatorModels';
import { refusedModels, type ModelStatus } from '@/lib/api/modelConnections';
import type { TranslationKey } from '@/lib/i18n/translate';

import {
  initialFields,
  isAccessDenied,
  newProvidedConnection,
  providedChanges,
  providedFailure,
  providedFieldProblems,
  providedNeedsVerification,
  refusedFields,
  type ProvidedField,
  type ProvidedFields,
} from './providedModelsForm';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

type Translate = ReturnType<typeof useT>;
type Problems = Partial<Record<ProvidedField, true>>;

const PROBLEM_KEYS: Record<ProvidedField, TranslationKey> = {
  name: 'adminModels.invalid.name',
  base_url: 'connections.invalid.url',
  api_key: 'connections.invalid.key',
  models: 'connections.invalid.model',
  default_model: 'adminModels.invalid.defaultModel',
  end_user_header: 'adminModels.invalid.header',
  user_concurrency: 'adminModels.invalid.userConcurrency',
};

/** What a save answered: the connection, and what could not be done after it was saved. */
export interface SavedConnection {
  connection: OperatorModelConnection;
  /** The connection was saved, but making it the default for new users failed. */
  problem?: string;
}

function actionLabel(busy: boolean, verify: boolean, t: Translate): string {
  if (busy) return t(verify ? 'connections.verifyingShort' : 'connections.busy');
  return t(verify ? 'connections.verifySave' : 'connections.save');
}

/**
 * The save itself. A create answers a disabled connection; making it the
 * default for new users is an edit, sent once the create has succeeded, and a
 * failure there keeps the connection and is reported beside it.
 */
async function submit(
  connection: OperatorModelConnection | undefined,
  fields: ProvidedFields,
  t: Translate,
): Promise<SavedConnection | null> {
  if (connection) {
    const changes = providedChanges(connection, fields);
    // Closing an unchanged editor sends nothing.
    if (!Object.keys(changes).length) return null;
    return { connection: await updateOperatorModelConnection(connection.id, changes) };
  }
  const created = await createOperatorModelConnection(newProvidedConnection(fields));
  if (!fields.is_default) return { connection: created };
  try {
    const updated = await updateOperatorModelConnection(created.id, { is_default: true });
    return { connection: updated };
  } catch (cause) {
    // An ended session is the page's to handle, not a note beside the card.
    if (isAccessDenied(cause)) throw cause;
    return {
      connection: created,
      problem: t('adminModels.form.defaultFailed', { reason: providedFailure(cause, t) }),
    };
  }
}

/** A labelled control with its hint and its field-level problem, if any. */
function Field({
  label,
  hint,
  problem,
  children,
}: {
  label: string;
  hint?: string;
  problem: string;
  children: (
    inputId: string,
    describedBy: string | undefined,
    invalid: true | undefined,
  ) => ReactNode;
}) {
  const id = useId();
  const described = [problem ? `${id}-problem` : '', hint ? `${id}-hint` : '']
    .filter(Boolean)
    .join(' ');
  return (
    <div>
      <label htmlFor={`${id}-input`} className="block text-sm font-medium">
        {label}
      </label>
      {children(`${id}-input`, described || undefined, problem ? true : undefined)}
      {problem ? (
        <p id={`${id}-problem`} className="mt-1 text-xs leading-5 text-status-error">
          {problem}
        </p>
      ) : null}
      {hint ? (
        <p id={`${id}-hint`} className="mt-1 text-xs leading-5 text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Add or edit a provided connection: a name, a Base URL, a write-only key and
 * model IDs, then which model new users start on, whether this connection is
 * where they start, the header that names an account to the provider, and how
 * many requests one account may have in flight.
 *
 * **Verify and save** checks every model with the provider before anything is
 * stored, which can take minutes and cannot be cancelled once sent; a refusal
 * keeps every input, with each model's result on its chip. A new connection is
 * saved disabled: it is enabled from its card once every model has a default
 * allowance.
 */
export function ProvidedConnectionForm({
  connection,
  onSaved,
  onCancel,
  onAccessDenied,
}: {
  connection?: OperatorModelConnection;
  /** `null`: nothing was changed. */
  onSaved: (saved: SavedConnection | null) => void;
  onCancel: () => void;
  onAccessDenied?: (cause: unknown) => void;
}) {
  const t = useT();
  const [fields, setFields] = useState<ProvidedFields>(() => initialFields(connection));
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [problems, setProblems] = useState<Problems>({});
  const [results, setResults] = useState<Record<string, ModelStatus>>({});
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // A model ID still in the line when the form is saved counts too.
  const models = withModel(fields.models, draft);
  const current: ProvidedFields = {
    ...fields,
    name: fields.name.trim(),
    base_url: fields.base_url.trim(),
    key: fields.key.trim(),
    models,
    default_model: models.includes(fields.default_model) ? fields.default_model : '',
    end_user_header: fields.end_user_header.trim(),
  };
  const verify = providedNeedsVerification(connection, current);
  const problem = (field: ProvidedField) => (problems[field] ? t(PROBLEM_KEYS[field]) : '');

  /** Any edit after a refusal: its results described the form as it was. */
  function edit(change: Partial<ProvidedFields>) {
    setFields((value) => ({ ...value, ...change }));
    setResults({});
    setProblems({});
    setError('');
  }

  function failed(cause: unknown) {
    if (isAccessDenied(cause)) onAccessDenied?.(cause);
    const refused = refusedModels(cause) ?? [];
    setFields((value) => ({ ...value, models }));
    setDraft('');
    setResults(Object.fromEntries(refused.map((status) => [status.model, status])));
    setProblems(refusedFields(cause));
    setError(providedFailure(cause, t));
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const found = providedFieldProblems(current, !connection);
    const invalid = Object.keys(found).length > 0;
    setProblems(found);
    setResults({});
    setError(invalid ? t('connections.invalid') : '');
    if (invalid) return;
    setBusy(true);
    try {
      const saved = await submit(connection, current, t);
      if (mounted.current) onSaved(saved);
    } catch (cause) {
      if (mounted.current) failed(cause);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const incomplete =
    !current.name || !current.base_url || !models.length || (!connection && !current.key);

  return (
    <form
      onSubmit={(event) => void save(event)}
      noValidate
      aria-label={t(connection ? 'adminModels.form.editTitle' : 'adminModels.form.addTitle')}
      className="mt-4 space-y-4 rounded-xl border border-border p-4"
    >
      <fieldset disabled={busy} className="space-y-4">
        <legend className="text-sm font-semibold">
          {t(connection ? 'adminModels.form.editTitle' : 'adminModels.form.addTitle')}
        </legend>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label={t('adminModels.form.name')}
            hint={t('adminModels.form.nameHint')}
            problem={problem('name')}
          >
            {(inputId, describedBy, invalid) => (
              <input
                id={inputId}
                className={inputClass}
                value={fields.name}
                onChange={(event) => edit({ name: event.target.value })}
                maxLength={OPERATOR_MAX_NAME_CHARS}
                autoComplete="off"
                aria-invalid={invalid}
                aria-describedby={describedBy}
              />
            )}
          </Field>
          <Field label={t('connections.url')} problem={problem('base_url')}>
            {(inputId, describedBy, invalid) => (
              <input
                id={inputId}
                className={inputClass}
                type="url"
                value={fields.base_url}
                onChange={(event) => edit({ base_url: event.target.value })}
                maxLength={2048}
                placeholder="https://api.example.com/v1"
                autoComplete="off"
                spellCheck={false}
                aria-invalid={invalid}
                aria-describedby={describedBy}
              />
            )}
          </Field>
        </div>
        <Field
          label={t('connections.key')}
          hint={t('adminModels.form.keyHint')}
          problem={problem('api_key')}
        >
          {(inputId, describedBy, invalid) => (
            <PasswordInput
              id={inputId}
              className={inputClass}
              toggleLabels={{ show: t('connections.showKey'), hide: t('connections.hideKey') }}
              value={fields.key}
              onChange={(event) => edit({ key: event.target.value })}
              maxLength={4096}
              autoComplete="new-password"
              spellCheck={false}
              placeholder={connection ? t('connections.keyKept') : undefined}
              aria-invalid={invalid}
              aria-describedby={describedBy}
            />
          )}
        </Field>
        <ModelIdsField
          models={fields.models}
          draft={draft}
          max={OPERATOR_MAX_MODELS}
          results={results}
          invalid={Boolean(problems.models)}
          disabled={busy}
          onModels={(next) => edit({ models: next })}
          onDraft={(next) => {
            setDraft(next);
            edit({});
          }}
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label={t('adminModels.form.defaultModel')}
            hint={t('adminModels.form.defaultModelHint')}
            problem={problem('default_model')}
          >
            {(inputId, describedBy, invalid) => (
              <Select
                id={inputId}
                className={inputClass}
                value={current.default_model}
                onValueChange={(value) => edit({ default_model: value })}
                options={[
                  { value: '', label: t('adminModels.form.noDefaultModel') },
                  ...models.map((model) => ({ value: model, label: model })),
                ]}
                aria-invalid={invalid}
                aria-describedby={describedBy}
              />
            )}
          </Field>
          <Field
            label={t('adminModels.form.userConcurrency')}
            hint={t('adminModels.form.userConcurrencyHint', { max: MAX_USER_CONCURRENCY })}
            problem={problem('user_concurrency')}
          >
            {(inputId, describedBy, invalid) => (
              <input
                id={inputId}
                className={inputClass}
                inputMode="numeric"
                value={fields.user_concurrency}
                onChange={(event) => edit({ user_concurrency: event.target.value })}
                maxLength={3}
                autoComplete="off"
                aria-invalid={invalid}
                aria-describedby={describedBy}
              />
            )}
          </Field>
        </div>
        <Field
          label={t('adminModels.form.header')}
          hint={t('adminModels.form.headerHint')}
          problem={problem('end_user_header')}
        >
          {(inputId, describedBy, invalid) => (
            <input
              id={inputId}
              className={inputClass}
              value={fields.end_user_header}
              onChange={(event) => edit({ end_user_header: event.target.value })}
              maxLength={64}
              placeholder="X-End-User"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={invalid}
              aria-describedby={describedBy}
            />
          )}
        </Field>
        <label className="flex min-h-11 items-start gap-3 text-sm">
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 shrink-0"
            checked={fields.is_default}
            onChange={(event) => edit({ is_default: event.target.checked })}
          />
          <span>
            <span className="font-medium">{t('adminModels.form.isDefault')}</span>
            <span className="block text-xs leading-5 text-muted">
              {t('adminModels.form.isDefaultHint')}
            </span>
          </span>
        </label>
      </fieldset>
      <div role="status" aria-live="polite">
        {busy && verify ? (
          <p className="text-sm text-muted">{t('adminModels.form.verifying')}</p>
        ) : null}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-status-error">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          disabled={busy || incomplete}
          aria-busy={busy || undefined}
          className="ui-button ui-button-primary min-h-11"
        >
          {actionLabel(busy, verify, t)}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="ui-button ui-button-secondary min-h-11"
        >
          {t('connections.cancel')}
        </button>
      </div>
      <p className="text-xs leading-5 text-muted">
        {t(connection ? 'adminModels.form.verifyNote' : 'adminModels.form.createNote')}
      </p>
    </form>
  );
}
