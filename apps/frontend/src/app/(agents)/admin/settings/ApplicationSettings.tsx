'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  getAgentAdminSettings,
  setAgentAdminSettings,
  type AdminSettingDefinition,
  type AdminSettingValue,
  type AgentAdminSettings,
} from '@/lib/api/adminSettings';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

const INPUT_CLASS =
  'w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-700 shadow-sm disabled:opacity-40 aria-[invalid=true]:border-red-400 dark:border-white/10 dark:bg-gray-950 dark:text-gray-200';
const BUTTON_CLASS =
  'rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5';

function displayValue(value: AdminSettingValue): string {
  return typeof value === 'boolean' ? (value ? 'Enabled' : 'Disabled') : String(value);
}

function parseValue(
  definition: AdminSettingDefinition,
  draft: string,
): { value: AdminSettingValue } | { error: string } {
  if (definition.kind === 'boolean') return { value: draft === 'true' };
  if (definition.kind === 'string') {
    if (definition.choices?.length && !definition.choices.includes(draft)) {
      return { error: 'Choose one of the available values.' };
    }
    return { value: draft };
  }
  const value = Number(draft);
  if (draft.trim() === '' || !Number.isFinite(value)) return { error: 'Enter a number.' };
  if (definition.kind === 'integer' && !Number.isInteger(value)) {
    return { error: 'Enter a whole number.' };
  }
  if (definition.minimum !== undefined && value < definition.minimum) {
    return { error: `Enter a value of at least ${definition.minimum}.` };
  }
  if (definition.maximum !== undefined && value > definition.maximum) {
    return { error: `Enter a value no greater than ${definition.maximum}.` };
  }
  return { value };
}

function SettingField({
  definition,
  value,
  configured,
  busy,
  onSave,
}: {
  definition: AdminSettingDefinition;
  value: AdminSettingValue | null | undefined;
  configured: boolean;
  busy: boolean;
  onSave: (key: string, value: AdminSettingValue | null) => void;
}) {
  const incoming = definition.secret ? '' : String(value ?? definition.default);
  const [baseline, setBaseline] = useState(incoming);
  const [draft, setDraft] = useState(incoming);
  const [error, setError] = useState<string | null>(null);
  const [multiline, setMultiline] = useState(incoming.includes('\n'));
  const dirty = draft !== baseline;
  // Refresh pristine fields, while preserving drafts in this and other rows.
  useEffect(() => {
    if (incoming === baseline) return;
    if (draft === baseline) {
      setDraft(incoming);
      if (incoming.includes('\n')) setMultiline(true);
    }
    setBaseline(incoming);
  }, [incoming, baseline, draft]);

  const id = `application-setting-${definition.key}`;
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const inputProps = {
    id,
    name: definition.key,
    value: draft,
    disabled: busy,
    'aria-invalid': error !== null,
    'aria-describedby': `${helpId}${error ? ` ${errorId}` : ''}`,
    className: INPUT_CLASS,
    onChange: (
      event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
    ) => {
      setDraft(event.target.value);
      setError(null);
    },
  };

  return (
    <div className="border-t border-gray-100 py-4 first:border-t-0 dark:border-white/5">
      <form
        noValidate
        className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(200px,320px)]"
        onSubmit={(event) => {
          event.preventDefault();
          if (busy || !dirty) return;
          const parsed = parseValue(definition, draft);
          if ('error' in parsed) {
            setError(parsed.error);
            return;
          }
          setError(null);
          onSave(definition.key, parsed.value);
        }}
      >
        <div>
          <label htmlFor={id} className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {definition.label}
          </label>
          <p id={helpId} className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
            {definition.description}
          </p>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {definition.secret
              ? configured
                ? 'Configured. Saved values are hidden.'
                : 'Not configured. Saved values are hidden.'
              : `Default: ${displayValue(definition.default) || '(empty)'}`}
          </p>
        </div>
        <div>
          {definition.kind === 'boolean' ? (
            <select {...inputProps}>
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </select>
          ) : definition.choices?.length ? (
            <select {...inputProps}>
              {!definition.choices.includes(draft) && <option value={draft}>{draft}</option>}
              {definition.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice || '(empty)'}
                </option>
              ))}
            </select>
          ) : definition.kind === 'string' && multiline ? (
            <textarea {...inputProps} rows={4} autoComplete="off" spellCheck={false} />
          ) : (
            <input
              {...inputProps}
              type={
                definition.secret ? 'password' : definition.kind === 'string' ? 'text' : 'number'
              }
              autoComplete={definition.secret ? 'new-password' : 'off'}
              step={definition.kind === 'integer' ? 1 : 'any'}
              min={definition.minimum}
              max={definition.maximum}
              placeholder={
                definition.secret ? (configured ? 'Replace saved value' : 'Enter value') : undefined
              }
            />
          )}
          {definition.kind === 'string' && !definition.choices?.length && (
            <label className="mt-2 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              <input
                type="checkbox"
                checked={multiline}
                disabled={busy}
                onChange={(event) => setMultiline(event.target.checked)}
                aria-label={`Use multiple lines for ${definition.label}`}
              />
              Multiple lines
            </label>
          )}
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="submit"
              disabled={busy || !dirty}
              aria-label={`Save ${definition.label}`}
              className={BUTTON_CLASS}
            >
              Save
            </button>
            <button
              type="button"
              disabled={busy}
              aria-label={`Reset ${definition.label}`}
              onClick={() => onSave(definition.key, null)}
              className={BUTTON_CLASS}
            >
              Reset
            </button>
          </div>
          {error && (
            <p id={errorId} role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">
              {error}
            </p>
          )}
        </div>
      </form>
    </div>
  );
}

export default function ApplicationSettings({
  onAccessDenied,
}: {
  onAccessDenied?: (cause: unknown) => void;
}) {
  const [snapshot, setSnapshot] = useState<AgentAdminSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saved, setSaved] = useState<Record<string, number>>({});
  const mounted = useRef(false);
  const writing = useRef(false);
  const revoked = useRef(false);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);

  const fail = useCallback(
    (cause: unknown) => {
      if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
        revoked.current = true;
        request.current?.abort();
        sequence.current += 1;
        setSnapshot(null);
        setError(null);
        setNotice(null);
        setDenied(true);
        onAccessDenied?.(cause);
      } else {
        setError(getErrorMessage(cause));
      }
    },
    [onAccessDenied],
  );

  const load = useCallback(async () => {
    if (writing.current || revoked.current) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    try {
      const next = await getAgentAdminSettings(controller.signal);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      setSnapshot(next);
      setError(null);
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      fail(cause);
    } finally {
      if (mounted.current && current === sequence.current) setLoading(false);
    }
  }, [fail]);

  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 30_000);
    return () => {
      mounted.current = false;
      request.current?.abort();
      clearInterval(timer);
    };
  }, [load]);

  const save = async (key: string, value: AdminSettingValue | null) => {
    if (writing.current || revoked.current) return;
    writing.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    try {
      const next = await setAgentAdminSettings({ [key]: value }, controller.signal);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      setSnapshot(next);
      // Only the saved row resets its draft; unrelated edits stay intact.
      setSaved((previous) => ({ ...previous, [key]: (previous[key] ?? 0) + 1 }));
      const label = next.definitions.find((definition) => definition.key === key)?.label ?? key;
      setNotice(value === null ? `${label} reset to its default.` : `${label} saved.`);
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      fail(cause);
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  if (denied) {
    return (
      <p role="alert" className="mt-8 text-sm text-red-600 dark:text-red-400">
        Admin access required.
      </p>
    );
  }
  const groups = new Map<string, AdminSettingDefinition[]>();
  for (const definition of snapshot?.definitions ?? []) {
    const category = definition.category || 'General';
    groups.set(category, [...(groups.get(category) ?? []), definition]);
  }

  return (
    <section className="mt-8 rounded-2xl border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-900">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">
          Application settings
        </h2>
        <button
          type="button"
          disabled={busy || loading}
          onClick={() => void load()}
          className={BUTTON_CLASS}
        >
          Refresh settings
        </button>
      </div>
      <p className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">
        Saved settings apply across the deployment. Reset restores the built-in default.
      </p>
      {loading && (
        <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          Loading application settings…
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="mt-4 flex items-center justify-between gap-3 text-sm text-red-600 dark:text-red-400"
        >
          <span>{error}</span>
          <button
            type="button"
            disabled={busy}
            onClick={() => void load()}
            className={BUTTON_CLASS}
          >
            Retry application settings
          </button>
        </div>
      )}
      {notice && (
        <p role="status" className="mt-4 text-sm text-gray-600 dark:text-gray-300">
          {notice}
        </p>
      )}
      {[...groups].map(([category, definitions]) => (
        <div key={category} className="mt-6">
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{category}</h3>
          {definitions.map((definition) => (
            <SettingField
              key={`${definition.key}:${saved[definition.key] ?? 0}`}
              definition={definition}
              value={snapshot?.values[definition.key]}
              configured={snapshot?.secret_configured[definition.key] ?? false}
              busy={busy}
              onSave={(key, value) => void save(key, value)}
            />
          ))}
        </div>
      ))}
      {snapshot && groups.size === 0 && (
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          No application settings are available.
        </p>
      )}
      {(snapshot?.bootstrap.length ?? 0) > 0 && (
        <details className="mt-6 border-t border-gray-100 pt-4 dark:border-white/5">
          <summary className="cursor-pointer text-sm font-medium text-gray-900 dark:text-gray-100">
            Deployment setup
          </summary>
          <p className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">
            These settings are supplied when the service starts. Their values are hidden here.
          </p>
          <dl className="mt-3 space-y-3">
            {snapshot?.bootstrap.map((definition) => (
              <div key={definition.key}>
                <dt className="text-xs font-medium text-gray-700 dark:text-gray-200">
                  {definition.label}{' '}
                  <code className="ml-1 font-normal text-gray-500 dark:text-gray-400">
                    {definition.key}
                  </code>
                </dt>
                <dd className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {definition.description}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </section>
  );
}
