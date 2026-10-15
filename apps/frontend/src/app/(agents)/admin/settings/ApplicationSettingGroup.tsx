'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { useModalDialog } from '@/components/ui/useModalDialog';
import type { AdminSettingDefinition, AdminSettingValue } from '@/lib/api/adminSettings';

import type { ApplicationSettingsState } from './ApplicationSettings';
import { ARCHIVE_BUCKET, displayValue, parseValue } from './settingValues';

type Changes = Record<string, AdminSettingValue | null>;
type Review = { changes: Changes; reset?: AdminSettingDefinition };
const INPUT_CLASS =
  'ui-input min-h-11 w-full disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-status-error';
const BUTTON_CLASS = 'ui-button ui-button-secondary min-h-11';

export function SettingsIcon({
  kind,
  className = 'h-4 w-4',
}: {
  kind: 'lock' | 'link' | 'settings' | 'review';
  className?: string;
}) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      {kind === 'lock' ? (
        <>
          <rect x="5" y="10" width="14" height="11" rx="2.5" />
          <path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2" />
        </>
      ) : kind === 'link' ? (
        <>
          <path
            d="m10 13 4-4m-5 7-1 1a4.25 4.25 0 0 1-6-6l4-4a4.25 4.25 0 0 1 6 0m0 10a4.25 4.25 0 0 0 6 0l4-4a4.25 4.25 0 0 0-6-6l-1 1"
            transform="translate(1 0) scale(.92)"
          />
        </>
      ) : kind === 'review' ? (
        <>
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Z" />
          <path d="M14 3v6h6m-12 6 2.5 2.5L16 12" />
        </>
      ) : (
        <>
          <path d="M4 7h6m4 0h6M4 17h10m4 0h2" />
          <circle cx="12" cy="7" r="2" />
          <circle cx="16" cy="17" r="2" />
        </>
      )}
    </svg>
  );
}

function ReviewChanges({
  review,
  settings,
  onClose,
  onConfirm,
  returnFocus,
}: {
  review: Review;
  settings: ApplicationSettingsState;
  onClose: () => void;
  onConfirm: () => void;
  returnFocus: React.RefObject<HTMLElement>;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  const title = review.reset
    ? review.reset.secret
      ? 'Clear saved secret?'
      : 'Restore default value?'
    : 'Review changes';
  useModalDialog(panel, {
    busy: settings.busy,
    onClose,
    initialFocus: '[data-cancel]',
    returnFocus,
  });
  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4 backdrop-blur-[2px]">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-description`}
        className="ui-card max-h-[calc(100dvh-2rem)] w-full max-w-2xl overflow-y-auto rounded-2xl p-0 shadow-2xl"
      >
        <div className="flex items-center gap-3 px-5 pt-5 sm:px-6 sm:pt-6">
          <span
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${review.reset ? 'bg-status-error/10 text-status-error' : 'bg-surface-muted text-foreground'}`}
          >
            <SettingsIcon kind={review.reset ? 'lock' : 'review'} className="h-5 w-5" />
          </span>
          <h2 id={`${id}-title`} className="text-lg font-semibold tracking-tight text-foreground">
            {title}
          </h2>
          {!review.reset && (
            <span className="ml-auto shrink-0 rounded-full bg-surface-muted px-2.5 py-1 text-xs text-muted">
              {Object.keys(review.changes).length}{' '}
              {Object.keys(review.changes).length === 1 ? 'change' : 'changes'}
            </span>
          )}
        </div>
        <p id={`${id}-description`} className="mt-3 px-5 text-sm leading-6 text-muted sm:px-6">
          {review.reset?.secret
            ? 'This removes the saved credential. Features that use it may stop working until a new credential is configured.'
            : 'These changes apply across the deployment after settings synchronize. Check the values before applying.'}
        </p>
        <div className="mx-5 my-5 divide-y divide-border sm:mx-6 sm:my-6">
          {Object.entries(review.changes).map(([key, value]) => {
            const definition = settings.snapshot?.definitions.find((field) => field.key === key);
            if (!definition) return null;
            const current = settings.snapshot?.values[key] ?? definition.default;
            const next = value === null ? definition.default : value;
            return (
              <div key={key} className="py-4 first:pt-0 last:pb-0">
                <p className="text-sm font-medium text-foreground">{definition.label}</p>
                <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] sm:items-stretch">
                  <div className="min-w-0 rounded-xl bg-surface-muted/80 p-3.5">
                    <dt className="mb-2 text-xs font-medium text-muted">Current value</dt>
                    <dd className="whitespace-pre-wrap break-words text-[13px] leading-6 text-muted [overflow-wrap:anywhere]">
                      {definition.secret
                        ? settings.snapshot?.secret_configured[key]
                          ? 'Configured · hidden'
                          : 'Not configured'
                        : displayValue(current) || '(empty)'}
                    </dd>
                  </div>
                  <span aria-hidden="true" className="hidden items-center px-1 text-muted sm:flex">
                    <svg
                      viewBox="0 0 20 20"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      className="h-4 w-4"
                    >
                      <path d="M4 10h12m-5-5 5 5-5 5" />
                    </svg>
                  </span>
                  <div
                    className={`min-w-0 rounded-xl border p-3.5 ${review.reset ? 'border-status-error/20 bg-status-error/5' : 'border-border-strong bg-surface'}`}
                  >
                    <dt className="mb-2 text-xs font-medium text-muted">
                      {value === null ? 'Default value' : 'New value'}
                    </dt>
                    <dd className="whitespace-pre-wrap break-words text-[13px] font-medium leading-6 text-foreground [overflow-wrap:anywhere]">
                      {definition.secret
                        ? value === null
                          ? 'Not configured — secret will be cleared'
                          : 'New secret · hidden'
                        : displayValue(next) || '(empty) — this setting will be cleared'}
                    </dd>
                  </div>
                </dl>
              </div>
            );
          })}
        </div>
        {settings.error && (
          <p role="alert" className="mx-5 mb-5 text-sm text-status-error sm:mx-6">
            {settings.error}
          </p>
        )}
        <div className="flex flex-wrap justify-end gap-2 border-t border-border bg-surface-muted/50 px-5 py-4 sm:px-6">
          <button
            data-cancel
            type="button"
            disabled={settings.busy}
            onClick={onClose}
            className={BUTTON_CLASS}
          >
            {review.reset ? 'Cancel' : 'Back to editing'}
          </button>
          <button
            type="button"
            disabled={settings.busy}
            onClick={onConfirm}
            className={`ui-button min-h-11 ${review.reset ? 'bg-status-error text-white hover:opacity-90' : 'ui-button-primary'}`}
          >
            {settings.busy
              ? 'Applying…'
              : review.reset?.secret
                ? 'Clear secret'
                : review.reset
                  ? 'Restore default'
                  : 'Apply changes'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function ApplicationSettingGroup({
  title,
  fields,
  settings,
}: {
  title: string;
  fields: AdminSettingDefinition[];
  settings: ApplicationSettingsState;
}) {
  const { snapshot, saved, busy, save } = settings;
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [replacing, setReplacing] = useState<Record<string, boolean>>({});
  const [multiline, setMultiline] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [review, setReview] = useState<Review | null>(null);
  const previousSaved = useRef(saved);
  const editButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const groupId = useId();
  const incoming = (field: AdminSettingDefinition) =>
    field.secret ? '' : String(snapshot?.values[field.key] ?? field.default);
  const changed = fields.filter(
    (field) =>
      !field.read_only && drafts[field.key] !== undefined && drafts[field.key] !== incoming(field),
  );

  // A save can also update a related field in another group (for example an S3 URI's prefix).
  // Clear only those drafts; all unrelated edits survive saves and category navigation.
  useEffect(() => {
    const keys = Object.keys(saved).filter((key) => saved[key] !== previousSaved.current[key]);
    previousSaved.current = saved;
    if (!keys.length) return;
    for (const setter of [setDrafts, setErrors]) {
      setter((previous) =>
        Object.fromEntries(Object.entries(previous).filter(([key]) => !keys.includes(key))),
      );
    }
    setReplacing((previous) =>
      Object.fromEntries(Object.entries(previous).filter(([key]) => !keys.includes(key))),
    );
  }, [saved]);

  useEffect(() => {
    if (!editing || !changed.length) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [editing, changed.length]);

  const cancel = () => {
    setDrafts({});
    setReplacing({});
    setMultiline({});
    setErrors({});
    setEditing(false);
    requestAnimationFrame(() => editButton.current?.focus());
  };

  const prepare = () => {
    if (busy || !changed.length) return;
    const changes: Changes = {};
    const additional: Changes = {};
    const invalid: Record<string, string> = {};
    for (const field of changed) {
      const parsed = parseValue(field, drafts[field.key]);
      if ('error' in parsed) invalid[field.key] = parsed.error;
      else {
        changes[field.key] = parsed.value;
        Object.assign(additional, parsed.additionalChanges);
      }
    }
    setErrors(invalid);
    if (Object.keys(invalid).length) {
      document.getElementById(`application-setting-${Object.keys(invalid)[0]}`)?.focus();
      return;
    }
    // The prefix encoded in an S3 URI takes precedence over a separate prefix draft.
    setReview({ changes: { ...changes, ...additional } });
  };

  const apply = async () => {
    if (!review || busy) return;
    const notice = review.reset
      ? `${review.reset.label} ${review.reset.secret ? 'cleared.' : 'restored to its default.'}`
      : `${title} settings saved.`;
    if (await save(review.changes, notice)) {
      if (!review.reset) {
        returnFocus.current = editButton.current;
        cancel();
      }
      setReview(null);
    }
  };

  return (
    <div
      className={`mt-4 rounded-xl border bg-surface first:mt-0 ${editing ? 'border-accent/30 shadow-[0_0_0_3px_rgb(var(--ui-accent)/0.04)]' : 'border-border'}`}
    >
      <div className="flex min-h-14 items-center justify-between gap-4 rounded-t-xl border-b border-border/70 bg-surface-muted/45 px-4 py-2 sm:px-5">
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-border/70 bg-surface text-muted">
            <SettingsIcon
              kind={
                fields.some((field) => field.secret)
                  ? 'lock'
                  : fields.some((field) => /url|uri/.test(field.key))
                    ? 'link'
                    : 'settings'
              }
            />
          </span>
          <h4 id={groupId} className="text-sm font-semibold text-foreground">
            {title}
          </h4>
          {editing && (
            <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent">
              Editing
            </span>
          )}
        </div>
        <button
          ref={editButton}
          type="button"
          aria-label={`Edit ${title} settings`}
          disabled={busy || editing || fields.every((field) => field.read_only)}
          hidden={editing}
          className={`ui-button ui-button-ghost min-h-9 shrink-0 gap-1.5 px-2.5 text-xs hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11 ${editing ? 'hidden' : ''}`}
          onClick={() => {
            setEditing(true);
            requestAnimationFrame(() =>
              form.current
                ?.querySelector<HTMLElement>(
                  'input:not([type="checkbox"]), select, textarea, [data-replace]',
                )
                ?.focus(),
            );
          }}
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className="h-4 w-4"
          >
            <path d="m12.5 4.5 3 3M4 16l3.5-.5L16 7a2.1 2.1 0 0 0-3-3l-8.5 8.5L4 16Z" />
          </svg>
          Edit
        </button>
      </div>
      <form
        ref={form}
        noValidate
        aria-labelledby={groupId}
        onSubmit={(event) => {
          event.preventDefault();
          prepare();
        }}
      >
        {fields.map((field) => {
          const key = field.key;
          const id = `application-setting-${key}`;
          const configured = snapshot?.secret_configured[key] ?? false;
          const editable = editing && !field.read_only;
          const showInput = editable && (!field.secret || replacing[key]);
          const value = drafts[key] ?? incoming(field);
          const error = errors[key];
          const inputProps = {
            id,
            name: key,
            value,
            disabled: busy,
            'aria-invalid': !!error,
            'aria-describedby': `${id}-help${error ? ` ${id}-error` : ''}`,
            className: INPUT_CLASS,
            onChange: (
              event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
            ) => {
              const next = event.target.value;
              setDrafts((previous) => {
                const updated = { ...previous };
                if (next === incoming(field)) delete updated[key];
                else updated[key] = next;
                return updated;
              });
              setErrors((previous) => ({ ...previous, [key]: '' }));
            },
          };
          return (
            <div
              key={key}
              className="grid items-start gap-3 border-b border-border/60 px-4 py-4 last:border-b-0 sm:px-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] lg:gap-8"
            >
              <div className="min-w-0">
                <label
                  id={`${id}-label`}
                  htmlFor={showInput ? id : undefined}
                  className="text-sm font-medium text-foreground"
                >
                  {field.label}
                </label>
                <p id={`${id}-help`} className="mt-1 break-words text-xs leading-5 text-muted">
                  {field.description}
                  {editable &&
                    key === ARCHIVE_BUCKET &&
                    ' Saving an S3 URI replaces the prefix; a bucket-root URI clears it.'}
                </p>
                {editable && !field.secret && (
                  <p className="mt-2 break-words text-xs text-muted">
                    Default: {displayValue(field.default) || '(empty)'}
                  </p>
                )}
              </div>
              <div className="min-w-0">
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    {!showInput ? (
                      <div
                        id={id}
                        tabIndex={-1}
                        aria-labelledby={`${id}-label`}
                        aria-describedby={`${id}-help`}
                        className={`ui-focus-ring whitespace-pre-wrap break-words rounded-lg text-[13px] leading-6 text-foreground [overflow-wrap:anywhere] ${field.secret ? 'py-1' : 'bg-surface-muted/70 px-3 py-2 font-mono'}`}
                      >
                        {field.secret ? (
                          <span
                            className={`inline-flex items-center gap-2 rounded-full px-2.5 py-1 text-xs font-medium ${configured ? 'bg-status-success/10 text-status-success' : 'bg-surface-muted text-muted'}`}
                          >
                            <span
                              aria-hidden="true"
                              className="h-1.5 w-1.5 rounded-full bg-current"
                            />
                            {configured ? 'Configured · value hidden' : 'Not configured'}
                          </span>
                        ) : (
                          displayValue(snapshot?.values[key] ?? field.default) || (
                            <span className="text-muted">Not set</span>
                          )
                        )}
                        {field.read_only && (
                          <span className="ml-2 text-xs text-muted">Read only</span>
                        )}
                      </div>
                    ) : field.kind === 'boolean' ? (
                      <select {...inputProps}>
                        <option value="true">Enabled</option>
                        <option value="false">Disabled</option>
                      </select>
                    ) : field.choices?.length ? (
                      <select {...inputProps}>
                        {!field.choices.includes(value) && <option value={value}>{value}</option>}
                        {field.choices.map((choice) => (
                          <option key={choice} value={choice}>
                            {choice || '(empty)'}
                          </option>
                        ))}
                      </select>
                    ) : field.kind === 'string' && (multiline[key] ?? value.includes('\n')) ? (
                      <textarea
                        {...inputProps}
                        rows={4}
                        autoComplete="off"
                        spellCheck={false}
                        className={`${INPUT_CLASS} resize-y`}
                      />
                    ) : (
                      <input
                        {...inputProps}
                        type={
                          field.secret ? 'password' : field.kind === 'string' ? 'text' : 'number'
                        }
                        autoComplete={field.secret ? 'new-password' : 'off'}
                        spellCheck={false}
                        step={field.kind === 'integer' ? 1 : 'any'}
                        min={field.minimum}
                        max={field.maximum}
                        placeholder={field.secret ? 'Enter new secret' : undefined}
                      />
                    )}
                  </div>
                  {editable && (
                    <details className="relative shrink-0">
                      <summary
                        aria-label={`More actions for ${field.label}`}
                        className="ui-button ui-button-ghost flex h-11 w-11 cursor-pointer list-none items-center justify-center text-lg [&::-webkit-details-marker]:hidden"
                        onClick={(event) => {
                          if (busy) event.preventDefault();
                        }}
                      >
                        ···
                      </summary>
                      <div className="absolute right-0 z-20 mt-1 w-52 rounded-xl border border-border bg-surface-raised p-1 shadow-lg">
                        <button
                          type="button"
                          disabled={busy || (field.secret && !configured)}
                          className="ui-button ui-button-ghost min-h-11 w-full justify-start text-status-error"
                          aria-label={`${field.secret ? 'Clear' : 'Restore default for'} ${field.label}`}
                          onClick={(event) => {
                            const details = event.currentTarget.closest('details');
                            returnFocus.current = details?.querySelector('summary') ?? null;
                            if (details) details.open = false;
                            setReview({ changes: { [key]: null }, reset: field });
                          }}
                        >
                          {field.secret ? 'Clear secret…' : 'Restore default…'}
                        </button>
                      </div>
                    </details>
                  )}
                </div>
                {editable && field.secret && !replacing[key] && (
                  <button
                    data-replace
                    type="button"
                    disabled={busy}
                    className={`${BUTTON_CLASS} mt-2`}
                    aria-label={`${configured ? 'Replace' : 'Set'} ${field.label}`}
                    onClick={() => {
                      setReplacing((previous) => ({ ...previous, [key]: true }));
                      requestAnimationFrame(() => document.getElementById(id)?.focus());
                    }}
                  >
                    {configured ? 'Replace secret' : 'Set secret'}
                  </button>
                )}
                {showInput && field.kind === 'string' && !field.choices?.length && (
                  <label className="mt-2 flex min-h-9 cursor-pointer items-center gap-2 text-xs text-muted">
                    <input
                      type="checkbox"
                      checked={multiline[key] ?? value.includes('\n')}
                      disabled={busy}
                      onChange={(event) =>
                        setMultiline((previous) => ({ ...previous, [key]: event.target.checked }))
                      }
                      aria-label={`Use multiple lines for ${field.label}`}
                      className="ui-focus-ring h-4 w-4 rounded accent-accent"
                    />
                    Multiple lines
                  </label>
                )}
                {error && (
                  <p id={`${id}-error`} role="alert" className="mt-2 text-sm text-status-error">
                    {error}
                  </p>
                )}
              </div>
            </div>
          );
        })}
        {editing && (
          <div className="flex flex-wrap items-center justify-end gap-2 rounded-b-xl border-t border-border bg-surface-muted/60 px-4 py-3 sm:px-5">
            <span className="mr-auto text-xs text-muted">
              {changed.length
                ? `${changed.length} unsaved ${changed.length === 1 ? 'change' : 'changes'}`
                : 'No changes yet'}
            </span>
            <button
              type="button"
              disabled={busy}
              className={BUTTON_CLASS}
              onClick={cancel}
              aria-label={`Cancel editing ${title}`}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy || !changed.length}
              className="ui-button ui-button-primary min-h-11"
              aria-label={`Review ${title} changes`}
              onClick={(event) => {
                returnFocus.current = event.currentTarget;
              }}
            >
              Review changes
            </button>
          </div>
        )}
      </form>
      {review && (
        <ReviewChanges
          review={review}
          settings={settings}
          onClose={() => setReview(null)}
          onConfirm={() => void apply()}
          returnFocus={returnFocus}
        />
      )}
    </div>
  );
}
