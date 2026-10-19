'use client';

import { useEffect, useId, useRef, type ReactNode, type Ref } from 'react';

import type {
  BlockedReason,
  ComputeAllocation,
  ComputeField,
  ComputeOperation,
} from '@/lib/api/compute';

export const inputClass =
  'ui-input min-h-11 w-full disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-status-error';
export const buttonClass = 'ui-button ui-button-secondary min-h-11';
export const primaryClass = 'ui-button ui-button-primary min-h-11';
export const dangerClass =
  'ui-button min-h-11 border border-status-error/25 bg-status-error/10 text-status-error hover:bg-status-error/20';

export function Panel({
  title,
  onClose,
  onBack,
  titleRef,
  actions,
  children,
}: {
  title: string;
  onClose: () => void;
  onBack?: () => void;
  titleRef?: Ref<HTMLHeadingElement>;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (typeof element.showModal === 'function') element.showModal();
    else element.setAttribute('open', '');
    return () => element.close?.();
  }, []);
  return (
    <dialog
      ref={dialog}
      aria-label={title}
      onCancel={onClose}
      className="ui-card m-auto max-h-[calc(100dvh-1rem)] w-[min(42rem,calc(100vw-1rem))] max-w-none overflow-y-auto p-0 shadow-xl backdrop:bg-black/40 dark:backdrop:bg-black/60 sm:max-h-[calc(100dvh-2rem)] sm:w-[min(42rem,calc(100vw-2rem))]"
    >
      <div className="sticky top-0 z-10 flex items-start justify-between gap-3 border-b border-border bg-surface-raised px-4 py-4 sm:px-6">
        <div className="min-w-0">
          {onBack && (
            <button
              type="button"
              className="ui-button ui-button-ghost mb-2 min-h-11 -ml-3"
              onClick={onBack}
            >
              ← Back
            </button>
          )}
          <h2
            ref={titleRef}
            tabIndex={titleRef ? -1 : undefined}
            className="ui-focus-ring break-words rounded text-lg font-semibold tracking-tight leading-8"
          >
            {title}
          </h2>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {actions}
          <button
            className="ui-icon-button ui-button-ghost h-11 w-11"
            type="button"
            onClick={onClose}
            aria-label="Close panel"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 20 20"
              className="h-5 w-5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path d="m5 5 10 10M5 15 15 5" />
            </svg>
          </button>
        </div>
      </div>
      <div className="p-4 sm:p-6">{children}</div>
    </dialog>
  );
}

export function Reasons({ reasons }: { reasons: BlockedReason[] }) {
  if (!reasons.length) return null;
  return (
    <ul className="mt-3 space-y-2 rounded-xl border border-status-warning/20 bg-status-warning/5 p-4 text-sm leading-6 text-status-warning">
      {reasons.map((reason, index) => (
        <li key={`${reason.code}-${index}`}>{reason.message}</li>
      ))}
    </ul>
  );
}

export function ErrorNotice({ error }: { error: string | null }) {
  return error ? (
    <p
      role="alert"
      className="my-3 rounded-xl border border-status-error/20 bg-status-error/5 p-4 text-sm leading-6 text-status-error"
    >
      {error}
    </p>
  ) : null;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-2 text-sm text-foreground">
      <span className="font-medium">{label}</span>
      {children}
    </label>
  );
}

export function initialFields(fields: ComputeField[]): Record<string, string> {
  return Object.fromEntries(
    fields.map((field) => [
      field.key,
      field.default === undefined
        ? ''
        : field.type === 'json'
          ? JSON.stringify(field.default, null, 2)
          : field.type === 'string_list' && Array.isArray(field.default)
            ? field.default.join('\n')
            : String(field.default),
    ]),
  );
}

export function parseStringList(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[,\r\n]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

export class ProviderFieldError extends Error {
  constructor(
    public field: string,
    message: string,
  ) {
    super(message);
  }
}

export function parseFields(
  fields: ComputeField[],
  values: Record<string, string>,
): Record<string, unknown> {
  return Object.fromEntries(
    fields
      .filter((field) =>
        field.type === 'string_list' ? values[field.key] !== undefined : values[field.key]?.trim(),
      )
      .map((field) => {
        const raw = values[field.key];
        if (field.max_length !== undefined && raw.length > field.max_length) {
          throw new ProviderFieldError(
            field.key,
            `${field.label} must be ${field.max_length} characters or fewer.`,
          );
        }
        if (field.type === 'json') {
          try {
            return [field.key, JSON.parse(raw)];
          } catch {
            throw new ProviderFieldError(field.key, `${field.label} must contain valid JSON.`);
          }
        }
        if (field.type === 'string_list') return [field.key, parseStringList(raw)];
        return [field.key, field.type === 'number' ? Number(raw) : raw];
      }),
  );
}

export function ProviderFields({
  fields,
  values,
  onChange,
  errors = {},
}: {
  fields: ComputeField[];
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  errors?: Record<string, string>;
}) {
  const id = useId();
  return (
    <>
      {fields.map((field) => {
        const isTextArea = ['json', 'textarea', 'string_list'].includes(field.type);
        const common = {
          className: inputClass,
          'aria-invalid': Boolean(errors[field.key]) || undefined,
          'aria-describedby':
            [
              field.description && `${id}-${field.key}-help`,
              errors[field.key] && `${id}-${field.key}-error`,
            ]
              .filter(Boolean)
              .join(' ') || undefined,
          required: field.required,
          value: values[field.key] ?? '',
          onChange: (
            event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
          ) => onChange({ ...values, [field.key]: event.target.value }),
        };
        return (
          <div key={field.key}>
            <Field label={field.label}>
              {field.type === 'select' ? (
                <select {...common}>
                  <option value="">Select…</option>
                  {field.options?.map((option) => (
                    <option value={option.value} key={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : isTextArea ? (
                <textarea {...common} rows={4} spellCheck={false} />
              ) : (
                <input
                  {...common}
                  type={field.type === 'number' ? 'number' : 'text'}
                  min={field.min}
                  max={field.max}
                  maxLength={field.max_length}
                  autoComplete="off"
                />
              )}
            </Field>
            {field.description && (
              <span
                id={`${id}-${field.key}-help`}
                className="mt-1.5 block text-xs leading-5 text-muted"
              >
                {field.description}
              </span>
            )}
            {errors[field.key] && (
              <span
                id={`${id}-${field.key}-error`}
                role="alert"
                className="mt-1.5 block text-xs leading-5 text-status-error"
              >
                {errors[field.key]}
              </span>
            )}
          </div>
        );
      })}
    </>
  );
}

export function absoluteTime(value: string | null | undefined): string {
  if (!value) return 'Not observed';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'Unknown';
  return date.toLocaleString(undefined, { timeZoneName: 'short' });
}

/** An absent provider resource that never existed: no identity or node was ever recorded. */
export function neverCreated(allocation: ComputeAllocation): boolean {
  return (
    allocation.state === 'absent' &&
    allocation.provider_id !== 'existing_ssh' &&
    !allocation.external_id &&
    allocation.nodes.length === 0
  );
}

export function remainingTime(value: string): string {
  const minutes = Math.ceil((new Date(value).getTime() - Date.now()) / 60_000);
  if (minutes <= 0) return 'Deadline passed';
  if (minutes < 60) return `${minutes}m remaining`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m remaining`;
}

export function OperationDetails({ operation }: { operation: ComputeOperation }) {
  return (
    <div className="space-y-3 break-words text-sm leading-6" aria-live="polite">
      <p className="rounded-lg border border-border bg-surface-muted px-3 py-2 font-semibold text-foreground">
        {operation.kind}: {operation.state}
      </p>
      <p>Current step: {operation.step}</p>
      {operation.message && <p>{operation.message}</p>}
      {typeof operation.result?.target_expires_at === 'string' && (
        <p>Requested expiry: {absoluteTime(operation.result.target_expires_at)}</p>
      )}
      {typeof operation.result?.actual_expires_at === 'string' && (
        <p>Observed expiry: {absoluteTime(operation.result.actual_expires_at)}</p>
      )}
      {Boolean(operation.result?.checks) && typeof operation.result?.checks === 'object' && (
        <ul className="list-inside list-disc">
          {Object.entries(operation.result.checks ?? {}).map(([key, value]) => (
            <li key={key}>
              {typeof value === 'string'
                ? value
                : `${key.replaceAll('_', ' ')}: ${value ? 'passed' : 'not ready'}`}
            </li>
          ))}
        </ul>
      )}
      <Reasons reasons={operation.blocked_reasons} />
      <p className="text-xs text-muted">Last update: {absoluteTime(operation.updated_at)}</p>
      <p className="border-t border-border pt-3 text-xs text-muted">
        You can close this panel. The operation will continue.
      </p>
    </div>
  );
}
