'use client';

import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useT } from '@/components/providers';
import {
  createModelConnection,
  invalidFields,
  MAX_MODELS,
  refusedModels,
  updateModelConnection,
  type ModelConnection,
  type ModelStatus,
} from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';
import { ModelIdsField } from './ModelIdsField';
import type { Operate } from './connectionOps';
import {
  connectionChanges,
  fieldProblems,
  needsVerification,
  newConnection,
  withModel,
  type ConnectionField,
  type ConnectionFields,
} from './modelConnectionChanges';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

type Translate = ReturnType<typeof useT>;
type Problems = Partial<Record<ConnectionField, true>>;

const PROBLEM_KEYS = {
  name: 'connections.invalid.name',
  base_url: 'connections.invalid.url',
  api_key: 'connections.invalid.key',
  models: 'connections.invalid.model',
} as const satisfies Record<ConnectionField, string>;

/** Why a save failed, in the owner's words; never the raw failure, which may echo input. */
function failureMessage(cause: unknown, t: Translate): string {
  if (refusedModels(cause)) return t('connections.refused');
  if (invalidFields(cause)) return t('connections.invalid');
  if (cause instanceof APIError) {
    if (cause.code === 'model_connection_check_limit') return t('connections.limited');
    if (cause.code === 'model_connection_changed') return t('connections.changed');
    if (cause.code === 'model_connection_name_taken') return t('connections.nameTaken');
    if (cause.code === 'model_connection_verification_timeout') return t('connections.timedOut');
  }
  return t('connections.error');
}

/** The fields a refused request named, as the form's own field names. */
function serverProblems(cause: unknown): Problems {
  const problems: Problems = {};
  for (const field of invalidFields(cause) ?? []) {
    const name = field.split('.')[0] as ConnectionField;
    if (name in PROBLEM_KEYS) problems[name] = true;
  }
  return problems;
}

/** Whether the form can be saved yet: every field filled, within the model limit. */
function incomplete(
  fields: ConnectionFields,
  connection: ModelConnection | undefined,
  maxModels: number,
): boolean {
  if (!fields.name || !fields.base_url || !fields.models.length) return true;
  return fields.models.length > maxModels || (!connection && !fields.key);
}

function actionLabel(busy: boolean, verify: boolean, t: Translate): string {
  if (busy) return t(verify ? 'connections.verifyingShort' : 'connections.busy');
  return t(verify ? 'connections.verifySave' : 'connections.save');
}

/**
 * The save itself: verified by the server first when it reaches a new endpoint
 * or model. An edit runs through `operate`, so an answer that another change
 * to the same connection overtook is not applied (`undefined`).
 */
async function submit(
  connection: ModelConnection | undefined,
  fields: ConnectionFields,
  operate: Operate,
): Promise<ModelConnection | undefined> {
  if (!connection) return createModelConnection(newConnection(fields));
  const changes = connectionChanges(connection, fields);
  if (!Object.keys(changes).length) return connection;
  return operate(connection.id, () => updateModelConnection(connection.id, changes));
}

/** A labelled input with its field-level problem, if any. */
function Field({
  label,
  problem,
  children,
}: {
  label: string;
  problem: string;
  children: (describedBy: string | undefined) => ReactNode;
}) {
  const id = useId();
  return (
    <div>
      <label className="block text-sm font-medium">
        {label}
        {children(problem ? id : undefined)}
      </label>
      {problem ? (
        <p id={id} className="mt-1 text-xs leading-5 text-status-error">
          {problem}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Add or edit a connection: a name, a Base URL, a write-only key and model
 * IDs. **Verify and save** verifies before anything is stored; a refusal
 * keeps every input, with each model's result on its chip, to fix and retry.
 * A rename or removing models saves without calling the provider. While a
 * save runs it cannot be cancelled: the server may still commit it.
 */
export function ModelConnectionForm({
  connection,
  maxModels = MAX_MODELS,
  operate,
  onSaved,
  onCancel,
}: {
  connection?: ModelConnection;
  maxModels?: number;
  operate: Operate;
  /** `null`: saved, but another change overtook its answer; the page re-reads. */
  onSaved: (connection: ModelConnection | null) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const keyHint = useId();
  const [name, setName] = useState(connection?.name ?? '');
  const [url, setUrl] = useState(connection?.base_url ?? '');
  const [key, setKey] = useState('');
  const [models, setModels] = useState<string[]>(connection?.models ?? []);
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

  const fields: ConnectionFields = {
    name: name.trim(),
    base_url: url.trim(),
    models: withModel(models, draft),
    key: key.trim(),
  };
  const verify = needsVerification(connection, fields);
  const problem = (field: ConnectionField) => (problems[field] ? t(PROBLEM_KEYS[field]) : '');

  /** Any edit after a refusal: its results described the form as it was. */
  function edited<T>(set: (value: T) => void) {
    return (value: T) => {
      set(value);
      setResults({});
      setProblems({});
      setError('');
    };
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const found = fieldProblems(fields);
    if (!connection && !fields.key) found.api_key = true;
    const invalid = Object.keys(found).length > 0;
    setProblems(found);
    setResults({});
    setError(invalid ? t('connections.invalid') : '');
    if (invalid) return;
    setBusy(true);
    try {
      const saved = await submit(connection, fields, operate);
      if (!mounted.current) return;
      setKey('');
      onSaved(saved ?? null);
    } catch (cause) {
      if (!mounted.current) return;
      const refused = refusedModels(cause) ?? [];
      setModels(fields.models);
      setDraft('');
      setResults(Object.fromEntries(refused.map((status) => [status.model, status])));
      setProblems(serverProblems(cause));
      setError(failureMessage(cause, t));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void save(event)}
      noValidate
      className="mt-4 space-y-4 rounded-xl border border-border p-4"
    >
      <fieldset disabled={busy} className="space-y-4">
        <legend className="sr-only">
          {t(connection ? 'connections.edit' : 'connections.add')}
        </legend>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('connections.name')} problem={problem('name')}>
            {(describedBy) => (
              <input
                className={inputClass}
                value={name}
                onChange={(event) => edited(setName)(event.target.value)}
                maxLength={64}
                autoComplete="off"
                aria-invalid={describedBy ? true : undefined}
                aria-describedby={describedBy}
              />
            )}
          </Field>
          <Field label={t('connections.url')} problem={problem('base_url')}>
            {(describedBy) => (
              <input
                className={inputClass}
                type="url"
                value={url}
                onChange={(event) => edited(setUrl)(event.target.value)}
                maxLength={512}
                placeholder="https://api.example.com/v1"
                autoComplete="off"
                spellCheck={false}
                aria-invalid={describedBy ? true : undefined}
                aria-describedby={describedBy}
              />
            )}
          </Field>
        </div>
        <Field label={t('connections.key')} problem={problem('api_key')}>
          {(describedBy) => (
            <input
              className={inputClass}
              type="password"
              value={key}
              onChange={(event) => edited(setKey)(event.target.value)}
              maxLength={4096}
              autoComplete="new-password"
              spellCheck={false}
              placeholder={connection ? t('connections.keyKept') : undefined}
              aria-invalid={describedBy ? true : undefined}
              aria-describedby={describedBy ? `${describedBy} ${keyHint}` : keyHint}
            />
          )}
        </Field>
        <p id={keyHint} className="-mt-2 text-xs leading-5 text-muted">
          {t('connections.secretHint')}
        </p>
        <ModelIdsField
          models={models}
          draft={draft}
          max={maxModels}
          results={results}
          invalid={Boolean(problems.models)}
          disabled={busy}
          onModels={edited(setModels)}
          onDraft={edited(setDraft)}
        />
      </fieldset>
      <div role="status" aria-live="polite">
        {busy && verify ? <p className="text-sm text-muted">{t('connections.verifying')}</p> : null}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-status-error">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          disabled={busy || incomplete(fields, connection, maxModels)}
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
      {verify ? <p className="text-xs leading-5 text-muted">{t('connections.billing')}</p> : null}
    </form>
  );
}
