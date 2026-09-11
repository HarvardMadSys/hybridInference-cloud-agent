'use client';

// Shared viewers for the trajectory inspector.
//
// Everything rendered here is untrusted stored data: it is shown as text, never
// as HTML, and the copy actions copy exactly what was recorded.

import { useState, type ReactNode } from 'react';
import toast from 'react-hot-toast';

import { formatBytes } from './TurnFooter';

/** A stored timestamp, or the fact that none was recorded. */
export function recordedAt(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Not recorded';
  return new Date(value).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'medium' });
}

/** A recorded count. Null and missing mean the runtime did not record it. */
export function recordedCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('en-US')
    : 'Not recorded';
}

export async function copyText(text: string, what: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`${what} copied`);
  } catch {
    toast.error(`Could not copy the ${what.toLowerCase()}`);
  }
}

/** A titled block whose heading can jump to the tab that expands it. */
export function DetailSection({
  title,
  onOpen,
  children,
}: {
  title: string;
  onOpen?: () => void;
  children: ReactNode;
}) {
  return (
    <section className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
          {title}
        </h4>
        {onOpen ? (
          <button
            type="button"
            onClick={onOpen}
            className="rounded px-1 text-[11px] text-gray-400 transition-colors hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-100"
          >
            Open tab
          </button>
        ) : null}
      </div>
      {children}
    </section>
  );
}

export function DetailRows({ children }: { children: ReactNode }) {
  return <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-[12px]">{children}</dl>;
}

export function DetailRow({
  label,
  children,
  tone = 'default',
}: {
  label: string;
  children: ReactNode;
  tone?: 'default' | 'muted' | 'error';
}) {
  const value =
    tone === 'error'
      ? 'text-rose-600 dark:text-rose-400'
      : tone === 'muted'
        ? 'text-gray-400 dark:text-gray-500'
        : 'text-gray-800 dark:text-gray-200';
  return (
    <>
      <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className={`min-w-0 [overflow-wrap:anywhere] ${value}`}>{children}</dd>
    </>
  );
}

export function EmptyNote({ children }: { children: ReactNode }) {
  return <p className="text-[12px] text-gray-400 dark:text-gray-500">{children}</p>;
}

/** A recorded payload may have been cut by the platform; that stays visible. */
export function TruncationNote({
  payload,
  className = '',
}: {
  payload: Record<string, unknown>;
  className?: string;
}) {
  if (payload.truncated !== true && typeof payload.truncation_notice !== 'string') return null;
  const bytes = payload.original_payload_bytes;
  const size = typeof bytes === 'number' ? formatBytes(bytes) : null;
  return (
    <p
      role="note"
      className={`rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-800 dark:bg-amber-500/10 dark:text-amber-300 ${className}`}
    >
      Truncated by the platform{size ? ` at ${size}` : ''}. What is shown is everything that was
      stored, not the complete original payload.
    </p>
  );
}

function CopyRow({ text, what }: { text: string; what: string }) {
  return (
    <button
      type="button"
      onClick={() => void copyText(text, what)}
      className="rounded border border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
    >
      Copy
    </button>
  );
}

/** Plain text as recorded, in a block that scrolls rather than growing. */
export function TextBlock({
  text,
  what,
  maxHeight = 'max-h-80',
  empty,
}: {
  text: string;
  what: string;
  maxHeight?: string;
  empty?: string;
}) {
  if (!text) {
    return <EmptyNote>{empty ?? 'Nothing was recorded.'}</EmptyNote>;
  }
  return (
    <div className="space-y-1">
      <pre
        className={`${maxHeight} overflow-auto whitespace-pre-wrap break-words rounded-md bg-gray-50 p-2.5 font-mono text-[11px] leading-relaxed text-gray-800 dark:bg-gray-950 dark:text-gray-200`}
      >
        {text}
      </pre>
      <div className="flex justify-end">
        <CopyRow text={text} what={what} />
      </div>
    </div>
  );
}

/** Any recorded JSON value: a string stays a string, everything else is pretty-printed. */
export function ValueBlock({
  value,
  what,
  maxHeight = 'max-h-80',
}: {
  value: unknown;
  what: string;
  maxHeight?: string;
}) {
  if (typeof value === 'string') {
    return <TextBlock text={value} what={what} maxHeight={maxHeight} />;
  }
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? '';
  } catch {
    return <EmptyNote>This value could not be rendered.</EmptyNote>;
  }
  if (!text) return <EmptyNote>Nothing was recorded.</EmptyNote>;
  return (
    <div className="space-y-1">
      <pre
        className={`${maxHeight} overflow-auto rounded-md bg-gray-50 p-2.5 font-mono text-[11px] leading-relaxed text-gray-800 dark:bg-gray-950 dark:text-gray-200`}
      >
        {text}
      </pre>
      <div className="flex justify-end">
        <CopyRow text={text} what={what} />
      </div>
    </div>
  );
}

/** A stored event, collapsed, for the Raw tabs. */
export function StoredRecord({ title, event }: { title: string; event: unknown }) {
  const [open, setOpen] = useState(false);
  const text = JSON.stringify(event, null, 2) ?? '';
  return (
    <div className="rounded border border-gray-200 dark:border-white/10">
      <div className="flex items-center gap-2 px-2 py-1.5">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="min-w-0 flex-1 truncate text-left font-mono text-[11px] text-gray-600 dark:text-gray-300"
        >
          {title}
        </button>
        <CopyRow text={text} what="Event" />
      </div>
      {open ? (
        <pre className="max-h-80 overflow-auto border-t border-gray-100 bg-gray-50 p-2.5 font-mono text-[11px] leading-relaxed text-gray-800 dark:border-white/5 dark:bg-gray-950 dark:text-gray-200">
          {text}
        </pre>
      ) : null}
    </div>
  );
}

export function DownloadButton({ text, filename }: { text: string; filename: string }) {
  function download() {
    const blob = new Blob([text], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <button
      type="button"
      onClick={download}
      className="rounded border border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
    >
      Download
    </button>
  );
}

/** A tab strip with the ARIA relationships the design requires. */
export function TabStrip<T extends string>({
  idPrefix,
  tabs,
  active,
  onSelect,
}: {
  idPrefix: string;
  tabs: Array<{ key: T; label: string }>;
  active: T;
  onSelect: (tab: T) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Step detail views"
      className="flex gap-1 overflow-x-auto border-b border-gray-200 px-3 dark:border-white/10"
      onKeyDown={(event) => {
        const keys = tabs.map((tab) => tab.key);
        const index = keys.indexOf(active);
        let next = -1;
        if (event.key === 'ArrowRight') next = (index + 1) % keys.length;
        if (event.key === 'ArrowLeft') next = (index - 1 + keys.length) % keys.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = keys.length - 1;
        if (next < 0) return;
        event.preventDefault();
        onSelect(keys[next]);
        const target = document.getElementById(`${idPrefix}-tab-${keys[next]}`);
        target?.focus();
      }}
    >
      {tabs.map((tab) => (
        <button
          key={tab.key}
          id={`${idPrefix}-tab-${tab.key}`}
          type="button"
          role="tab"
          aria-selected={active === tab.key}
          aria-controls={`${idPrefix}-panel-${tab.key}`}
          tabIndex={active === tab.key ? 0 : -1}
          onClick={() => onSelect(tab.key)}
          className={`shrink-0 border-b-2 px-2.5 py-2 text-[12px] transition-colors ${
            active === tab.key
              ? 'border-gray-900 font-medium text-gray-900 dark:border-gray-100 dark:text-gray-50'
              : 'border-transparent text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
          }`}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({
  idPrefix,
  tab,
  children,
}: {
  idPrefix: string;
  tab: string;
  children: ReactNode;
}) {
  return (
    <div
      id={`${idPrefix}-panel-${tab}`}
      role="tabpanel"
      aria-labelledby={`${idPrefix}-tab-${tab}`}
      tabIndex={0}
      className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 text-[12px] focus:outline-none"
    >
      {children}
    </div>
  );
}
