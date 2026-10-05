'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';

import type { BlockedReason, ComputeField, ComputeOperation } from '@/lib/api/compute';

export const inputClass =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-white/20 dark:bg-gray-900 dark:text-gray-100';
const buttonBase =
  'rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/20';
export const buttonClass = `${buttonBase} hover:bg-gray-100 dark:hover:bg-white/10`;
export const primaryClass = `${buttonBase} bg-crimson text-white hover:bg-crimson/90 dark:hover:bg-crimson/90`;

export function Panel({
  title,
  onClose,
  onBack,
  actions,
  children,
}: {
  title: string;
  onClose: () => void;
  onBack?: () => void;
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
      className="m-auto max-h-[calc(100dvh-2rem)] w-[min(42rem,calc(100vw-2rem))] max-w-none overflow-y-auto rounded-2xl border border-gray-200 bg-white p-6 text-gray-900 shadow-xl backdrop:bg-black/30 dark:backdrop:bg-black/60 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"
    >
      <div className="mb-5 flex items-center justify-between gap-4">
        <div>
          {onBack && (
            <button
              type="button"
              className="mb-2 block text-sm text-gray-500 hover:underline dark:text-gray-400"
              onClick={onBack}
            >
              ← Back
            </button>
          )}
          <h2 className="text-lg font-semibold">{title}</h2>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {actions}
          <button
            className="rounded-lg p-2 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/5"
            type="button"
            onClick={onClose}
            aria-label="Close panel"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 20 20"
              className="h-4 w-4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path d="m5 5 10 10M5 15 15 5" />
            </svg>
          </button>
        </div>
      </div>
      {children}
    </dialog>
  );
}

export function Reasons({ reasons }: { reasons: BlockedReason[] }) {
  if (!reasons.length) return null;
  return (
    <ul className="mt-2 space-y-1 text-sm text-amber-800 dark:text-amber-300">
      {reasons.map((reason, index) => (
        <li key={`${reason.code}-${index}`}>{reason.message}</li>
      ))}
    </ul>
  );
}

export function ErrorNotice({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="my-3 text-sm text-red-700 dark:text-red-300">
      {error}
    </p>
  ) : null;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1 text-sm">
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
          : String(field.default),
    ]),
  );
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
      .filter((field) => values[field.key]?.trim())
      .map((field) => {
        const raw = values[field.key];
        if (field.type === 'json') {
          try {
            return [field.key, JSON.parse(raw)];
          } catch {
            throw new ProviderFieldError(field.key, `${field.label} must contain valid JSON.`);
          }
        }
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
              ) : field.type === 'json' || field.type === 'textarea' ? (
                <textarea {...common} rows={4} spellCheck={false} />
              ) : (
                <input
                  {...common}
                  type={field.type === 'number' ? 'number' : 'text'}
                  min={field.min}
                  max={field.max}
                  autoComplete="off"
                />
              )}
            </Field>
            {field.description && (
              <span
                id={`${id}-${field.key}-help`}
                className="block text-xs text-gray-500 dark:text-gray-400"
              >
                {field.description}
              </span>
            )}
            {errors[field.key] && (
              <span
                id={`${id}-${field.key}-error`}
                role="alert"
                className="block text-xs text-red-700 dark:text-red-300"
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

export function remainingTime(value: string): string {
  const minutes = Math.ceil((new Date(value).getTime() - Date.now()) / 60_000);
  if (minutes <= 0) return 'Deadline passed';
  if (minutes < 60) return `${minutes}m remaining`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m remaining`;
}

export function OperationDetails({ operation }: { operation: ComputeOperation }) {
  return (
    <div className="space-y-2 text-sm" aria-live="polite">
      <p className="font-medium">
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
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Last update: {absoluteTime(operation.updated_at)}
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        You can close this panel. The operation will continue.
      </p>
    </div>
  );
}
