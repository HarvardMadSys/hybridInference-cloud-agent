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

import { SettingsSection } from '../SettingsSection';
import {
  applicationCategory,
  applicationSettingGroup,
  applicationSettingPriority,
} from './settingsOrganization';

const INPUT_CLASS =
  'ui-input min-h-11 w-full disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-status-error';
const BUTTON_CLASS = 'ui-button ui-button-secondary min-h-11';
const ARCHIVE_BUCKET = 'sandbox_archive.bucket';
const ARCHIVE_PREFIX = 'sandbox_archive.prefix';

type ParsedValue =
  | { value: AdminSettingValue; additionalChanges?: Record<string, AdminSettingValue> }
  | { error: string };

function parseArchiveBucket(draft: string): ParsedValue {
  if (!draft) return { value: '' };
  const uri = draft.startsWith('s3://');
  // Keep raw components: URL parsers can silently normalize unsafe path segments.
  const components = uri ? /^s3:\/\/([^/]+)(?:\/(.*))?$/.exec(draft) : null;
  const bucket = uri ? (components?.[1] ?? '') : draft;
  if (
    /\s/.test(draft) ||
    !/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(bucket) ||
    /^\d{1,3}(?:\.\d{1,3}){3}$/.test(bucket) ||
    /\.\.|\.-|-\./.test(bucket) ||
    /^(?:xn--|sthree-|amzn-s3-demo-)/.test(bucket) ||
    /(?:-s3alias|--ol-s3|\.mrap|--x-s3|--table-s3)$/.test(bucket)
  ) {
    return { error: 'Enter a valid S3 bucket name or an S3 URI such as s3://bucket/prefix.' };
  }
  if (!uri) return { value: bucket };
  const path = components?.[2] ?? '';
  const prefix = path.endsWith('/') ? path.slice(0, -1) : path;
  if (
    path.startsWith('/') ||
    path.includes('//') ||
    new TextEncoder().encode(prefix).length > 512 ||
    (prefix &&
      (!/^[a-zA-Z0-9_./-]+$/.test(prefix) ||
        prefix.split('/').some((part) => !part || part === '.' || part === '..')))
  ) {
    return {
      error:
        'Enter an S3 URI with a prefix of at most 512 bytes using letters, numbers, underscores, dots, hyphens, and single slashes. Do not use . or .. path segments, query strings, or fragments.',
    };
  }
  return { value: bucket, additionalChanges: { [ARCHIVE_PREFIX]: prefix } };
}

function displayValue(value: AdminSettingValue): string {
  return typeof value === 'boolean' ? (value ? 'Enabled' : 'Disabled') : String(value);
}

function parseValue(definition: AdminSettingDefinition, draft: string): ParsedValue {
  if (definition.kind === 'boolean') return { value: draft === 'true' };
  if (definition.kind === 'string') {
    if (definition.key === ARCHIVE_BUCKET) return parseArchiveBucket(draft);
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
  onSave: (
    key: string,
    value: AdminSettingValue | null,
    additionalChanges?: Record<string, AdminSettingValue>,
  ) => void;
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
        tabIndex={-1}
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
          onSave(definition.key, parsed.value, parsed.additionalChanges);
        }}
      >
        <div className="min-w-0">
          <label htmlFor={id} className="text-sm font-medium text-foreground">
            {definition.label}
          </label>
          <p id={helpId} className="mt-1.5 break-words text-sm leading-6 text-muted">
            {definition.description}
            {definition.key === ARCHIVE_BUCKET &&
              ' Saving an S3 URI replaces the prefix; a bucket-root URI clears it.'}
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

export interface ApplicationSettingsOptions {
  onAccessDenied?: (cause: unknown) => void;
  recoveryToken?: string;
  onRevealCategory?: (category: string) => void;
  enabled?: boolean;
}

export function useApplicationSettings({
  onAccessDenied,
  recoveryToken,
  onRevealCategory,
  enabled = true,
}: ApplicationSettingsOptions = {}) {
  const [snapshot, setSnapshot] = useState<AgentAdminSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saved, setSaved] = useState<Record<string, number>>({});
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [focusField, setFocusField] = useState<string | null>(null);
  const previousHash = useRef<string | null>(null);
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
    if (!enabled || writing.current || revoked.current) return;
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
  }, [enabled, fail, recoveryToken]);

  useEffect(() => {
    if (!enabled) {
      revoked.current = true;
      request.current?.abort();
      sequence.current += 1;
      setSnapshot(null);
      setNotice(null);
      setError(null);
      setFocusField(null);
      setDenied(true);
      return;
    }
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
  }, [enabled, load]);

  useEffect(() => {
    const revealHash = () => {
      const hash = window.location.hash;
      if (hash === previousHash.current) return;
      const prefix = '#application-setting-';
      if (!hash.startsWith(prefix)) {
        previousHash.current = hash;
        return;
      }
      if (!snapshot) return;
      previousHash.current = hash;
      let key: string;
      try {
        key = decodeURIComponent(hash.slice(prefix.length));
      } catch {
        return;
      }
      const definition = snapshot.definitions.find((field) => field.key === key);
      if (!definition) return;
      const category = applicationCategory(definition);
      setSelectedCategory(category);
      onRevealCategory?.(category);
      setFocusField(key);
    };
    revealHash();
    window.addEventListener('hashchange', revealHash);
    return () => window.removeEventListener('hashchange', revealHash);
  }, [snapshot, onRevealCategory]);

  const save = async (
    key: string,
    value: AdminSettingValue | null,
    additionalChanges?: Record<string, AdminSettingValue>,
  ) => {
    if (!enabled || writing.current || revoked.current) return;
    writing.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    const changes = { [key]: value, ...additionalChanges };
    try {
      const next = recoveryToken
        ? await setAgentAdminSettings(changes, controller.signal, recoveryToken)
        : await setAgentAdminSettings(changes, controller.signal);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      setSnapshot(next);
      // Reset every saved row to its canonical value; unrelated edits stay intact.
      setSaved((previous) => {
        const updated = { ...previous };
        for (const changedKey of Object.keys(changes)) {
          updated[changedKey] = (previous[changedKey] ?? 0) + 1;
        }
        return updated;
      });
      const label = next.definitions.find((definition) => definition.key === key)?.label ?? key;
      setNotice(
        additionalChanges && ARCHIVE_PREFIX in additionalChanges
          ? `${label} saved. Prefix set to ${additionalChanges[ARCHIVE_PREFIX] || '(empty)'}.`
          : value === null
            ? `${label} reset to its default.`
            : `${label} saved.`,
      );
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      fail(cause);
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const groups = new Map<string, AdminSettingDefinition[]>();
  for (const definition of snapshot?.definitions ?? []) {
    const category = applicationCategory(definition);
    groups.set(category, [...(groups.get(category) ?? []), definition]);
  }
  const currentCategory =
    selectedCategory && groups.has(selectedCategory)
      ? selectedCategory
      : groups.keys().next().value;

  const revealSetting = (key: string) => {
    const definition = snapshot?.definitions.find((field) => field.key === key);
    if (!definition) return;
    const category = applicationCategory(definition);
    setSelectedCategory(category);
    onRevealCategory?.(category);
    setFocusField(key);
  };

  const clearFocusField = useCallback(() => setFocusField(null), []);

  return {
    snapshot,
    loading,
    busy,
    denied,
    error,
    notice,
    saved,
    groups,
    currentCategory,
    selectCategory: setSelectedCategory,
    focusField,
    clearFocusField,
    load,
    save,
    revealSetting,
    recoveryToken,
  };
}

export type ApplicationSettingsState = ReturnType<typeof useApplicationSettings>;

export function categoryTabId(category: string): string {
  return `application-${encodeURIComponent(category)}`;
}

export function ApplicationSettingsHeader({
  settings,
  title = 'Application settings',
}: {
  settings: ApplicationSettingsState;
  title?: string;
}) {
  const { busy, loading, load } = settings;
  return (
    <>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-lg font-semibold tracking-tight text-foreground">{title}</h2>
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
    </>
  );
}

export function ApplicationSettingsFeedback({ settings }: { settings: ApplicationSettingsState }) {
  const { loading, error, busy, load, notice, snapshot, recoveryToken, revealSetting } = settings;
  return (
    <>
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
          className="mt-5 break-words rounded-xl border border-status-success/20 bg-status-success/5 p-4 text-sm text-status-success"
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
                        onClick={() => revealSetting(field.key)}
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
    </>
  );
}

export function ApplicationSettingsCategory({
  settings,
  category,
  active,
  showHeading = true,
}: {
  settings: ApplicationSettingsState;
  category: string;
  active: boolean;
  showHeading?: boolean;
}) {
  const { groups, snapshot, saved, busy, save, focusField, clearFocusField } = settings;
  const definitions = groups.get(category);
  const id = `application-settings-category-${encodeURIComponent(category)}`;

  const renderGroups = (priority: 'important' | 'advanced') => {
    const related = new Map<string, AdminSettingDefinition[]>();
    for (const definition of definitions ?? []) {
      if (applicationSettingPriority(definition) !== priority) continue;
      const group = applicationSettingGroup(definition);
      related.set(group, [...(related.get(group) ?? []), definition]);
    }
    if (related.size === 0) {
      return <p className="py-4 text-sm text-muted">No {priority} settings on this page.</p>;
    }
    return [...related].map(([group, fields]) => (
      <div key={group} className="mt-4 first:mt-0">
        <h4 className="border-b border-border py-3 text-sm font-semibold text-foreground">
          {group}
        </h4>
        {fields.map((definition) => (
          <SettingField
            key={`${definition.key}:${saved[definition.key] ?? 0}`}
            definition={definition}
            value={snapshot?.values[definition.key]}
            configured={snapshot?.secret_configured[definition.key] ?? false}
            busy={busy}
            onSave={(key, value, additionalChanges) => void save(key, value, additionalChanges)}
          />
        ))}
      </div>
    ));
  };

  useEffect(() => {
    if (!active || !focusField || !definitions?.some((field) => field.key === focusField)) return;
    const input = document.getElementById(`application-setting-${focusField}`);
    const advanced = input?.closest('details');
    if (advanced) advanced.open = true;
    const target = input?.matches(':disabled') ? input.closest('form') : input;
    target?.focus();
    input?.scrollIntoView?.({ block: 'center' });
    clearFocusField();
  }, [active, focusField, definitions, clearFocusField]);

  return (
    <section
      id={id}
      aria-labelledby={showHeading ? `${id}-heading` : undefined}
      className={showHeading ? 'overflow-hidden rounded-xl border border-border' : undefined}
      hidden={!active}
    >
      {showHeading && (
        <h3
          id={`${id}-heading`}
          className="border-b border-border bg-surface-muted px-4 py-3 text-sm font-semibold text-foreground sm:px-5"
        >
          {category}
        </h3>
      )}
      <div className={showHeading ? 'px-4 sm:px-5' : undefined}>
        <SettingsSection title="Important" description="Main settings for this page.">
          {renderGroups('important')}
        </SettingsSection>
        <SettingsSection title="Advanced" description="Connection details and operational tuning.">
          {renderGroups('advanced')}
        </SettingsSection>
      </div>
    </section>
  );
}

export function ApplicationDeploymentSetup({ settings }: { settings: ApplicationSettingsState }) {
  const { snapshot } = settings;
  return (
    <>
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
              <div
                key={definition.key}
                id={`application-bootstrap-${definition.key}`}
                tabIndex={-1}
              >
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
    </>
  );
}

export default function ApplicationSettings(options: ApplicationSettingsOptions) {
  const settings = useApplicationSettings(options);
  const { denied, snapshot, groups, currentCategory } = settings;
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

  return (
    <section className="ui-card mt-6 p-4 sm:p-6" aria-label="Application settings">
      <ApplicationSettingsHeader settings={settings} />
      <ApplicationSettingsFeedback settings={settings} />
      {groups.size > 0 && (
        <div className="mt-6 grid gap-6 lg:grid-cols-[180px_minmax(0,1fr)]">
          <nav
            aria-label="Application setting categories"
            className="flex gap-1 overflow-x-auto rounded-lg bg-surface-muted p-1 lg:flex-col lg:self-start"
          >
            {[...groups].map(([category, definitions]) => (
              <button
                key={category}
                type="button"
                aria-pressed={currentCategory === category}
                aria-controls={`application-settings-category-${encodeURIComponent(category)}`}
                onClick={() => settings.selectCategory(category)}
                className={`ui-focus-ring flex min-h-11 shrink-0 items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-sm font-medium transition-colors ${
                  currentCategory === category
                    ? 'bg-surface text-foreground shadow-sm'
                    : 'text-muted hover:text-foreground'
                }`}
              >
                <span>{category}</span>
                <span
                  aria-hidden="true"
                  className="rounded bg-surface-muted px-1.5 py-0.5 text-xs text-muted"
                >
                  {definitions.length}
                </span>
              </button>
            ))}
          </nav>
          <div className="min-w-0">
            {/* Keep fields mounted so switching categories preserves unsaved edits. */}
            {[...groups.keys()].map((category) => (
              <ApplicationSettingsCategory
                key={category}
                settings={settings}
                category={category}
                active={currentCategory === category}
              />
            ))}
          </div>
        </div>
      )}
      {snapshot && groups.size === 0 && (
        <p className="mt-5 rounded-xl bg-surface-muted p-4 text-sm text-muted">
          No application settings are available.
        </p>
      )}
      <ApplicationDeploymentSetup settings={settings} />
    </section>
  );
}
