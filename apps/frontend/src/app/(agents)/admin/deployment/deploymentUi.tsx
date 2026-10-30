'use client';

import { useCallback, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useModalDialog } from '@/components/ui/useModalDialog';

import { SettingsIcon } from '../settings/ApplicationSettingGroup';

// The Settings page's building blocks, so Deployment reads as the same product:
// a titled group card, label-and-control rows, quiet status pills and a review
// dialog before anything that changes a machine.

export const INPUT_CLASS =
  'ui-input min-h-11 w-full disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-status-error';
export const BUTTON_CLASS = 'ui-button ui-button-secondary min-h-11';
export const PRIMARY_CLASS = 'ui-button ui-button-primary min-h-11';
export const DANGER_CLASS = 'ui-button min-h-11 bg-status-error text-white hover:opacity-90';
export const TEXT_BUTTON_CLASS =
  'ui-button ui-button-ghost min-h-9 shrink-0 gap-1.5 px-2.5 text-xs hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11';

export type Run = (work: () => Promise<string | void>) => Promise<void>;

type IconKind = 'lock' | 'link' | 'settings' | 'review';

/** A group of related values with a header row, like a Settings group. */
export function GroupCard({
  title,
  icon = 'settings',
  badge,
  actions,
  description,
  children,
  id,
  className = '',
}: {
  title: ReactNode;
  icon?: IconKind;
  badge?: ReactNode;
  actions?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  id?: string;
  className?: string;
}) {
  const heading = useId();
  return (
    <section
      id={id}
      aria-labelledby={heading}
      className={`mt-4 min-w-0 rounded-xl border border-border bg-surface first:mt-0 ${className}`}
    >
      <div className="flex min-h-14 flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-t-xl border-b border-border/70 bg-surface-muted/45 px-4 py-2 sm:px-5">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-border/70 bg-surface text-muted">
            <SettingsIcon kind={icon} />
          </span>
          <h3 id={heading} className="min-w-0 break-words text-sm font-semibold text-foreground">
            {title}
          </h3>
          {badge}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {description && (
        <p className="border-b border-border/60 px-4 py-3 text-xs leading-5 text-muted sm:px-5">
          {description}
        </p>
      )}
      {children}
    </section>
  );
}

/** A titled page section holding group cards, like a Settings category. */
export function Panel({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="ui-card mt-6 min-w-0 p-4 sm:p-6" aria-label={title}>
      <h2 className="text-lg font-semibold tracking-tight text-foreground">{title}</h2>
      {description && <p className="mt-1 max-w-3xl text-sm leading-6 text-muted">{description}</p>}
      <div className="mt-5">{children}</div>
    </section>
  );
}

/** One value: its name and meaning on the left, its state or control on the right. */
export function FieldRow({
  label,
  help,
  htmlFor,
  labelId,
  children,
}: {
  label: ReactNode;
  help?: ReactNode;
  htmlFor?: string;
  labelId?: string;
  children?: ReactNode;
}) {
  const Label = htmlFor ? 'label' : 'p';
  return (
    <div className="grid min-w-0 items-start gap-3 border-b border-border/60 px-4 py-4 last:border-b-0 sm:px-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] lg:gap-8">
      <div className="min-w-0">
        <Label
          id={labelId}
          htmlFor={htmlFor}
          className="block break-words text-sm font-medium text-foreground"
        >
          {label}
        </Label>
        {help && <p className="mt-1 break-words text-xs leading-5 text-muted">{help}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** A value as the machine reports it, read-only. */
export function Value({ children, mono = false }: { children: ReactNode; mono?: boolean }) {
  return (
    <p
      className={`break-words rounded-lg bg-surface-muted/70 px-3 py-2 text-[13px] leading-6 text-foreground [overflow-wrap:anywhere] ${mono ? 'font-mono' : ''}`}
    >
      {children}
    </p>
  );
}

const PILL_TONES = {
  success: 'bg-status-success/10 text-status-success',
  warning: 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
  error: 'bg-status-error/10 text-status-error',
  accent: 'bg-accent/10 text-accent',
  neutral: 'border border-border bg-surface-muted/60 text-muted',
} as const;

export type Tone = keyof typeof PILL_TONES;

export function Pill({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${PILL_TONES[tone]}`}
    >
      {tone !== 'neutral' && (
        <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" />
      )}
      {children}
    </span>
  );
}

/** Placeholder text inside a card with nothing to list yet. */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-4 text-sm leading-6 text-muted sm:px-5">{children}</p>;
}

export function ErrorNotice({ children, onRetry }: { children: ReactNode; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
    >
      <span className="min-w-0 break-words">{children}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 font-medium underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson"
        >
          Retry
        </button>
      )}
    </div>
  );
}

export function StatusNotice({ children }: { children: ReactNode }) {
  return (
    <p
      role="status"
      className="mt-6 rounded-xl border border-border bg-surface-muted/60 px-4 py-3 text-sm text-foreground"
    >
      {children}
    </p>
  );
}

/** A value that is shown once: copy it now, it cannot be read again. */
export function OneTimeSecret({
  title,
  description,
  value,
  onDismiss,
}: {
  title: string;
  description: string;
  value: string;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div
      role="status"
      className="mt-6 rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-4 sm:px-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-foreground">{title}</p>
          <p className="mt-1 text-xs leading-5 text-muted">{description}</p>
        </div>
        <button type="button" className={TEXT_BUTTON_CLASS} onClick={onDismiss}>
          I have stored it
        </button>
      </div>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <code className="min-w-0 flex-1 break-all rounded-lg bg-surface px-3 py-2 font-mono text-[13px] text-foreground ring-1 ring-border">
          {value}
        </code>
        <button
          type="button"
          className={`${BUTTON_CLASS} shrink-0`}
          onClick={() => {
            void navigator.clipboard?.writeText(value).then(
              () => setCopied(true),
              () => setCopied(false),
            );
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </div>
  );
}

export interface Confirmation {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  /** When set, the action waits until this exact text is typed. */
  typed?: { label: string; expected: string };
  action: (typed: string) => void;
}

function ConfirmDialog({
  ask,
  onClose,
  returnFocus,
}: {
  ask: Confirmation;
  onClose: () => void;
  returnFocus: React.RefObject<HTMLElement>;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  const [typed, setTyped] = useState('');
  useModalDialog(panel, { onClose, initialFocus: '[data-cancel]', returnFocus });
  const ready = !ask.typed || typed === ask.typed.expected;
  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4 backdrop-blur-[2px]">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-description`}
        className="ui-card max-h-[calc(100dvh-2rem)] w-full max-w-lg overflow-y-auto rounded-2xl p-0 shadow-2xl"
      >
        <div className="flex items-center gap-3 px-5 pt-5 sm:px-6 sm:pt-6">
          <span
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${ask.danger ? 'bg-status-error/10 text-status-error' : 'bg-surface-muted text-foreground'}`}
          >
            <SettingsIcon kind={ask.danger ? 'lock' : 'review'} className="h-5 w-5" />
          </span>
          <h2 id={`${id}-title`} className="text-lg font-semibold tracking-tight text-foreground">
            {ask.title}
          </h2>
        </div>
        <div
          id={`${id}-description`}
          className="mt-3 space-y-3 px-5 text-sm leading-6 text-muted sm:px-6"
        >
          {ask.body}
        </div>
        {ask.typed && (
          <div className="mt-4 px-5 sm:px-6">
            <label htmlFor={`${id}-typed`} className="text-sm font-medium text-foreground">
              {ask.typed.label}
            </label>
            <input
              id={`${id}-typed`}
              className={`${INPUT_CLASS} mt-2`}
              value={typed}
              autoComplete="off"
              onChange={(event) => setTyped(event.target.value)}
            />
          </div>
        )}
        <div className="mt-6 flex flex-wrap justify-end gap-2 border-t border-border bg-surface-muted/50 px-5 py-4 sm:px-6">
          <button data-cancel type="button" onClick={onClose} className={BUTTON_CLASS}>
            Cancel
          </button>
          <button
            type="button"
            disabled={!ready}
            onClick={() => {
              onClose();
              ask.action(typed);
            }}
            className={ask.danger ? DANGER_CLASS : PRIMARY_CLASS}
          >
            {ask.confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** Ask before acting; focus returns to whatever opened the question. */
export function useConfirmation(): [(ask: Confirmation) => void, ReactNode] {
  const [ask, setAsk] = useState<Confirmation | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const confirm = useCallback((next: Confirmation) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setAsk(next);
  }, []);
  const dialog = ask ? (
    <ConfirmDialog
      ask={ask}
      onClose={() => setAsk(null)}
      returnFocus={opener as React.RefObject<HTMLElement>}
    />
  ) : null;
  return [confirm, dialog];
}

/** A pointer to the operations guide, linked when this site configures one. */
export function GuideLink({ section }: { section: string }) {
  const { branding } = useSiteConfig();
  const name = <>the operations guide, under “{section}”</>;
  return branding.docsUrl ? (
    <a
      href={branding.docsUrl}
      target="_blank"
      rel="noreferrer"
      className="font-medium text-accent underline underline-offset-2"
    >
      {name}
    </a>
  ) : (
    name
  );
}
