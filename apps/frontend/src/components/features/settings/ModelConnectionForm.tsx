'use client';

import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers';
import {
  createModelConnection,
  MAX_MODELS,
  refusedModels,
  updateModelConnection,
  type ModelConnection,
  type ModelStatus,
} from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';
import { ModelIdsField } from './ModelIdsField';
import {
  connectionChanges,
  needsVerification,
  newConnection,
  withModel,
  type ConnectionFields,
} from './modelConnectionChanges';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

type Translate = ReturnType<typeof useT>;

/** Why a save failed, in the owner's words; never the raw failure, which may echo input. */
function failureMessage(cause: unknown, t: Translate): string {
  if (refusedModels(cause)) return t('connections.refused');
  if (cause instanceof APIError) {
    if (cause.code === 'model_connection_check_limit') return t('connections.limited');
    if (cause.code === 'model_connection_changed') return t('connections.changed');
    if (cause.code === 'model_connection_name_taken') return t('connections.nameTaken');
  }
  return t('connections.error');
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

/** The save itself: verified by the server first when it reaches a new endpoint or model. */
async function submit(
  connection: ModelConnection | undefined,
  fields: ConnectionFields,
): Promise<ModelConnection | undefined> {
  if (!connection) return createModelConnection(newConnection(fields));
  const changes = connectionChanges(connection, fields);
  return Object.keys(changes).length ? updateModelConnection(connection.id, changes) : connection;
}

/**
 * Add or edit a connection: a name, a Base URL, a write-only key and model
 * IDs. **Verify and save** verifies before anything is stored; a refusal
 * keeps every input, with each model's result on its chip, to fix and retry.
 * A rename or removing models saves without calling the provider.
 */
export function ModelConnectionForm({
  connection,
  maxModels = MAX_MODELS,
  onSaved,
  onSettledAfterLeaving,
  onCancel,
}: {
  connection?: ModelConnection;
  maxModels?: number;
  onSaved: (connection: ModelConnection) => void;
  /**
   * A save still running when the form was cancelled or closed finished
   * anyway: the server may now hold it, so the page re-reads its list
   * rather than trusting or dropping this late answer.
   */
  onSettledAfterLeaving?: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  const id = useId();
  const [name, setName] = useState(connection?.name ?? '');
  const [url, setUrl] = useState(connection?.base_url ?? '');
  const [key, setKey] = useState('');
  const [models, setModels] = useState<string[]>(connection?.models ?? []);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [results, setResults] = useState<Record<string, ModelStatus>>({});
  // A save answered after Cancel, a delete or leaving the page is not applied
  // as such; the page re-reads what the server holds instead.
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

  /** Any edit after a refusal: its results described the form as it was. */
  function edited<T>(set: (value: T) => void) {
    return (value: T) => {
      set(value);
      setResults({});
      setError('');
    };
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    setResults({});
    try {
      const saved = await submit(connection, fields);
      if (!mounted.current) {
        onSettledAfterLeaving?.();
        return;
      }
      setKey('');
      if (saved) onSaved(saved);
    } catch (cause) {
      if (!mounted.current) return;
      const refused = refusedModels(cause) ?? [];
      setModels(fields.models);
      setDraft('');
      setResults(Object.fromEntries(refused.map((status) => [status.model, status])));
      setError(failureMessage(cause, t));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void save(event)}
      className="mt-4 space-y-4 rounded-xl border border-border p-4"
    >
      <fieldset disabled={busy} className="space-y-4">
        <legend className="sr-only">
          {t(connection ? 'connections.edit' : 'connections.add')}
        </legend>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm font-medium">
            {t('connections.name')}
            <input
              className={inputClass}
              value={name}
              onChange={(event) => edited(setName)(event.target.value)}
              required
              maxLength={64}
              autoComplete="off"
            />
          </label>
          <label className="block text-sm font-medium">
            {t('connections.url')}
            <input
              className={inputClass}
              type="url"
              value={url}
              onChange={(event) => edited(setUrl)(event.target.value)}
              required
              maxLength={512}
              placeholder="https://api.example.com/v1"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        </div>
        <label className="block text-sm font-medium">
          {t('connections.key')}
          <input
            className={inputClass}
            type="password"
            value={key}
            onChange={(event) => edited(setKey)(event.target.value)}
            required={!connection}
            maxLength={4096}
            autoComplete="new-password"
            spellCheck={false}
            placeholder={connection ? t('connections.keyKept') : undefined}
            aria-describedby={`${id}-key`}
          />
        </label>
        <p id={`${id}-key`} className="-mt-2 text-xs leading-5 text-muted">
          {t('connections.secretHint')}
        </p>
        <ModelIdsField
          models={models}
          draft={draft}
          max={maxModels}
          results={results}
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
        <button type="button" onClick={onCancel} className="ui-button ui-button-secondary min-h-11">
          {t('connections.cancel')}
        </button>
      </div>
      {verify ? <p className="text-xs leading-5 text-muted">{t('connections.billing')}</p> : null}
    </form>
  );
}
