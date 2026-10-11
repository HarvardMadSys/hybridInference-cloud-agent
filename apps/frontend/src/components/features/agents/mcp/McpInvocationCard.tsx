'use client';

import { useEffect, useState } from 'react';
import type { McpCapabilityKind, McpInvocationApi } from '@/lib/api/mcp';

/** What a held call does to its capability, as the card's sentence puts it. */
const INVOCATION_VERBS: Record<McpCapabilityKind, string> = {
  tool: 'call',
  prompt: 'use the prompt',
  resource: 'read',
  resource_template: 'read',
};

/** The name a held call's server goes by on its card. */
export function connectionName(invocation: McpInvocationApi): string {
  return invocation.connection_label ?? `Connection ${invocation.connection_id}`;
}

/** What the held call does, in the sentence its card and notices use. */
export function invocationAction(invocation: McpInvocationApi): string {
  return `${INVOCATION_VERBS[invocation.capability_kind] ?? 'call'} ${invocation.capability_name}`;
}

/**
 * The server's own hints about a capability, phrased for the person deciding.
 *
 * Only hints the server stated are shown: the protocol's defaults would label
 * every unannotated tool destructive, which says nothing about the tool.
 */
function capabilityHints(annotations: Record<string, unknown>): string[] {
  const hints: string[] = [];
  if (annotations.readOnlyHint === true) hints.push('Read-only');
  else if (
    annotations.destructiveHint === true ||
    (annotations.readOnlyHint === false && annotations.destructiveHint !== false)
  )
    hints.push('May modify or delete data');
  else if (annotations.readOnlyHint === false) hints.push('Makes changes');
  if (annotations.openWorldHint === true) hints.push('Reaches external services');
  return hints;
}

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;

/** A short duration: `45 s`, `4 min`, `2 h`. */
function duration(ms: number, round: (value: number) => number): string {
  if (ms < MINUTE) return `${Math.max(1, round(ms / SECOND))} s`;
  if (ms < HOUR) return `${round(ms / MINUTE)} min`;
  return `${round(ms / HOUR)} h`;
}

/** The reader's local date and time for a hover title, or the raw value. */
function localTime(iso: string): string {
  const time = Date.parse(iso);
  return Number.isFinite(time) ? new Date(time).toLocaleString() : iso;
}

/** When a held call was made and how long it has left, kept current while it waits. */
function HeldCallTiming({
  createdAt,
  expiresAt,
  now,
}: {
  createdAt: string | null;
  expiresAt: string | null;
  now: number;
}) {
  const created = Date.parse(createdAt ?? '');
  const expires = Date.parse(expiresAt ?? '');
  const parts: React.ReactNode[] = [];
  if (createdAt && Number.isFinite(created)) {
    const elapsed = now - created;
    parts.push(
      <time key="created" dateTime={createdAt} title={localTime(createdAt)}>
        Requested {elapsed < 10 * SECOND ? 'just now' : `${duration(elapsed, Math.floor)} ago`}
      </time>,
    );
  }
  if (expiresAt && Number.isFinite(expires)) {
    const left = expires - now;
    parts.push(
      <time key="expires" dateTime={expiresAt} title={localTime(expiresAt)}>
        {left > 0 ? `Expires in ${duration(left, Math.ceil)}` : 'Expired'}
      </time>,
    );
  }
  if (!parts.length) return null;
  return (
    <p className="mt-0.5 text-xs text-amber-800 dark:text-amber-300">
      {parts.flatMap((part, index) => (index ? [' · ', part] : [part]))}
    </p>
  );
}

/** What the server says the held capability is, and its hints. Untrusted text. */
function HeldCapabilityDetails({ invocation }: { invocation: McpInvocationApi }) {
  const annotations = invocation.capability_annotations ?? {};
  const title = typeof annotations.title === 'string' ? annotations.title.trim() : '';
  const description = invocation.capability_description?.trim() ?? '';
  const hints = capabilityHints(annotations);
  if (!description && !hints.length && (!title || title === invocation.capability_name)) {
    return null;
  }
  return (
    <div className="mt-2 text-xs text-gray-700 dark:text-gray-300">
      {title && title !== invocation.capability_name ? (
        <p className="font-medium">{title}</p>
      ) : null}
      {description ? (
        <p className="mt-0.5 max-h-24 overflow-y-auto whitespace-pre-line">{description}</p>
      ) : null}
      {hints.length ? (
        <p className="mt-1 text-gray-500 dark:text-gray-400">Server hints: {hints.join(' · ')}</p>
      ) : null}
    </div>
  );
}

/** The arguments a held call would be sent with, or why there are none to show. */
function HeldCallArguments({ invocation }: { invocation: McpInvocationApi }) {
  const { arguments: args } = invocation;
  return (
    <>
      <p className="mt-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-amber-800 dark:text-amber-300">
        Arguments
      </p>
      {args && Object.keys(args).length ? (
        <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-white/70 dark:bg-black/20 p-2 font-mono text-xs text-gray-800 dark:text-gray-200">
          {JSON.stringify(args, null, 2)}
        </pre>
      ) : (
        <p className="mt-1 text-xs text-gray-700 dark:text-gray-300">
          {args ? 'No arguments.' : 'The arguments are not readable on this deployment.'}
        </p>
      )}
    </>
  );
}

/** The same decision details in a conversation and the owner's inbox. */
export function McpInvocationCard({
  invocation,
  busy,
  onDecision,
  onAlwaysAllow,
  children,
}: {
  invocation: McpInvocationApi;
  busy: boolean;
  onDecision: (approve: boolean) => void;
  onAlwaysAllow?: () => void;
  children?: React.ReactNode;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), SECOND);
    return () => clearInterval(timer);
  }, []);
  const expires = Date.parse(invocation.expires_at ?? '');
  const expired = Number.isFinite(expires) && expires <= now;
  const canDecide = () => !busy && (!Number.isFinite(expires) || expires > Date.now());
  return (
    <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3 dark:border-amber-500/25 dark:bg-amber-500/10">
      {children}
      <p className="text-sm font-medium text-amber-900 dark:text-amber-200">
        {connectionName(invocation)} wants to {invocationAction(invocation)}
      </p>
      <HeldCallTiming
        createdAt={invocation.created_at}
        expiresAt={invocation.expires_at}
        now={now}
      />
      <HeldCapabilityDetails invocation={invocation} />
      <HeldCallArguments invocation={invocation} />
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy || expired}
          onClick={() => {
            if (canDecide()) onDecision(true);
          }}
          className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-60 dark:bg-gray-100 dark:text-gray-900"
        >
          Approve
        </button>
        {onAlwaysAllow ? (
          <button
            type="button"
            disabled={busy || expired}
            onClick={() => {
              if (canDecide()) onAlwaysAllow();
            }}
            title={`Approve, and let ${connectionName(invocation)} ${invocationAction(invocation)} without asking in this conversation`}
            className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-800 disabled:cursor-not-allowed disabled:opacity-60 dark:border-white/20 dark:text-gray-200"
          >
            Always allow
          </button>
        ) : null}
        <button
          type="button"
          disabled={busy || expired}
          onClick={() => {
            if (canDecide()) onDecision(false);
          }}
          className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-800 disabled:cursor-not-allowed disabled:opacity-60 dark:border-white/20 dark:text-gray-200"
        >
          Deny
        </button>
      </div>
    </div>
  );
}
