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
  'ui-input min-h-11 w-full disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-status-error';
const BUTTON_CLASS = 'ui-button ui-button-secondary min-h-11';

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
    if (
      draft &&
      definition.minimum_bytes &&
      new TextEncoder().encode(draft).length < definition.minimum_bytes
    ) {
      return { error: `Enter a value of at least ${definition.minimum_bytes} bytes.` };
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
    disabled: busy || definition.read_only,
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
    <div className="border-t border-border py-5 first:border-t-0">
      <form
        noValidate
        className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(240px,360px)] lg:gap-8"
        onSubmit={(event) => {
          event.preventDefault();
          if (busy || !dirty || definition.read_only) return;
          const parsed = parseValue(definition, draft);
          if ('error' in parsed) {
            setError(parsed.error);
            return;
          }
          setError(null);
          onSave(definition.key, parsed.value);
        }}
      >
        <div className="min-w-0">
          <label htmlFor={id} className="text-sm font-medium text-foreground">
            {definition.label}
          </label>
          <p id={helpId} className="mt-1.5 break-words text-sm leading-6 text-muted">
            {definition.description}
          </p>
          <p className="mt-2 break-words text-xs leading-5 text-muted">
            {definition.secret
              ? configured
                ? 'Configured. Saved values are hidden.'
                : 'Not configured. Saved values are hidden.'
              : `Default: ${displayValue(definition.default) || '(empty)'}`}
          </p>
        </div>
        <div className="min-w-0">
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
            <textarea
              {...inputProps}
              className={`${INPUT_CLASS} resize-y`}
              rows={4}
              autoComplete="off"
              spellCheck={false}
            />
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
            <label className="mt-2 flex min-h-9 cursor-pointer items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={multiline}
                disabled={busy || definition.read_only}
                onChange={(event) => setMultiline(event.target.checked)}
                aria-label={`Use multiple lines for ${definition.label}`}
                className="ui-focus-ring h-4 w-4 rounded accent-accent disabled:cursor-not-allowed"
              />
              Multiple lines
            </label>
          )}
          <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
            {dirty && <span className="mr-auto text-xs text-muted">Unsaved changes</span>}
            <button
              type="submit"
              disabled={busy || !dirty || definition.read_only}
              aria-label={`Save ${definition.label}`}
              className="ui-button ui-button-primary min-h-11 min-w-16"
            >
              Save
            </button>
            <button
              type="button"
              disabled={busy || definition.read_only}
              aria-label={`Reset ${definition.label}`}
              onClick={() => onSave(definition.key, null)}
              className="ui-button ui-button-ghost min-h-11"
            >
              Reset
            </button>
          </div>
          {error && (
            <p id={errorId} role="alert" className="mt-2 text-sm text-status-error">
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
  recoveryToken,
}: {
  onAccessDenied?: (cause: unknown) => void;
  recoveryToken?: string;
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
      const next = recoveryToken
        ? await getAgentAdminSettings(controller.signal, recoveryToken)
        : await getAgentAdminSettings(controller.signal);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      setSnapshot(next);
      setError(null);
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      fail(cause);
    } finally {
      if (mounted.current && current === sequence.current) setLoading(false);
    }
  }, [fail, recoveryToken]);

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
      const next = recoveryToken
        ? await setAgentAdminSettings({ [key]: value }, controller.signal, recoveryToken)
        : await setAgentAdminSettings({ [key]: value }, controller.signal);
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
      <p
        role="alert"
        className="mt-6 rounded-xl border border-status-error/20 bg-status-error/5 p-4 text-sm text-status-error"
      >
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
    <section className="ui-card mt-6 p-4 sm:p-6" aria-label="Application settings">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-lg font-semibold tracking-tight text-foreground">
          Application settings
        </h2>
        <button
          type="button"
          disabled={busy || loading}
          onClick={() => void load()}
          className={`${BUTTON_CLASS} self-start sm:self-auto`}
        >
          Refresh settings
        </button>
      </div>
      <p className="mt-2 text-sm leading-6 text-muted">
        Saved settings apply across the deployment. Reset restores the built-in default.
      </p>
      {loading && (
        <p role="status" className="mt-5 rounded-xl bg-surface-muted p-4 text-sm text-muted">
          Loading application settings…
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="mt-5 flex flex-col items-start gap-3 rounded-xl border border-status-error/20 bg-status-error/5 p-4 text-sm text-status-error sm:flex-row sm:items-center sm:justify-between"
        >
          <span className="min-w-0 break-words">{error}</span>
          <button
            type="button"
            disabled={busy}
            onClick={() => void load()}
            className={`${BUTTON_CLASS} shrink-0`}
          >
            Retry application settings
          </button>
        </div>
      )}
      {notice && (
        <p
          role="status"
          className="mt-5 rounded-xl border border-status-success/20 bg-status-success/5 p-4 text-sm text-status-success"
        >
          {notice}
        </p>
      )}
      {snapshot?.setup?.required && (
        <div
          role="status"
          className="mt-5 rounded-xl border border-status-warning/20 bg-status-warning/5 p-4 text-sm leading-6 text-status-warning"
        >
          <h3 className="font-semibold">Administrator setup required</h3>
          <p className="mt-1">Complete the missing settings below to enable these features.</p>
          {snapshot.setup.groups
            .filter((group) => group.needs_setup)
            .map((group) => (
              <div key={group.key} className="mt-3">
                <p className="font-medium">{group.label}</p>
                <ul className="mt-1 list-inside list-disc">
                  {group.missing.map((field) => (
                    <li key={field.key}>
                      <a
                        className="ui-focus-ring rounded underline underline-offset-4"
                        href={`#application-setting-${field.key}`}
                      >
                        {field.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          {snapshot.setup.bootstrap_missing.map((field) => (
            <p key={field.key} className="mt-3">
              Ask the deployment administrator to configure {field.label} before saving credentials.
            </p>
          ))}
        </div>
      )}
      {recoveryToken && snapshot?.setup && !snapshot.setup.required && (
        <p
          role="status"
          className="mt-5 rounded-xl border border-status-success/20 bg-status-success/5 p-4 text-sm text-status-success"
        >
          Required setup is complete. You can return to sign-in.
        </p>
      )}
      {[...groups].map(([category, definitions]) => (
        <div key={category} className="mt-6 overflow-hidden rounded-xl border border-border">
          <h3 className="border-b border-border bg-surface-muted px-4 py-3 text-sm font-semibold text-foreground sm:px-5">
            {category}
          </h3>
          <div className="px-4 sm:px-5">
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
        </div>
      ))}
      {snapshot && groups.size === 0 && (
        <p className="mt-5 rounded-xl bg-surface-muted p-4 text-sm text-muted">
          No application settings are available.
        </p>
      )}
      {(snapshot?.bootstrap.length ?? 0) > 0 && (
        <details className="mt-6 border-t border-border pt-4">
          <summary className="ui-focus-ring min-h-11 cursor-pointer rounded-lg py-3 text-sm font-medium text-foreground">
            Deployment setup
          </summary>
          <p className="mt-2 text-sm leading-6 text-muted">
            These settings are supplied when the service starts. Their values are hidden here.
          </p>
          <dl className="mt-3 space-y-3">
            {snapshot?.bootstrap.map((definition) => (
              <div key={definition.key}>
                <dt className="text-sm font-medium text-foreground">
                  {definition.label}{' '}
                  <code className="ml-1 break-all rounded bg-surface-muted px-1.5 py-0.5 text-xs font-normal text-muted">
                    {definition.key}
                  </code>
                </dt>
                <dd className="mt-1 text-sm leading-6 text-muted">{definition.description}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </section>
  );
}
