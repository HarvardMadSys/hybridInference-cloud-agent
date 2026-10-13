'use client';

import { useEffect, useRef, useState } from 'react';

import { getAdminSandbox, type AdminSandboxDetail } from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON } from '../runs/shared';
import type { SandboxObservation } from './present';
import {
  applySandboxAction,
  sandboxActionBlocked,
  sandboxActionState,
  type SandboxAction,
} from './sandboxActions';

const ACTIONS: { value: SandboxAction; label: string }[] = [
  { value: 'stop', label: 'Stop idle sandboxes' },
  { value: 'delete', label: 'Archive to S3 and delete' },
  { value: 'discard', label: 'Delete without archive' },
  { value: 'archive', label: 'Remove from inventory' },
  { value: 'restore', label: 'Restore to inventory' },
];
const CONCURRENCY = 4;

interface ReviewedSandbox {
  id: string;
  title: string;
  detail: AdminSandboxDetail | null;
  observedAtMs: number;
  error: string | null;
}

interface BatchResult {
  id: string;
  title: string;
  status: 'queued' | 'requested' | 'completed' | 'failed' | 'skipped';
  message: string;
}

async function runBounded<T>(items: T[], signal: AbortSignal, apply: (item: T) => Promise<void>) {
  let index = 0;
  await Promise.all(
    Array.from({ length: Math.min(CONCURRENCY, items.length) }, async () => {
      while (!signal.aborted && index < items.length) {
        const item = items[index++];
        await apply(item);
      }
    }),
  );
}

function blockedReason(item: ReviewedSandbox, kind: SandboxAction, nowMs: number): string | null {
  if (item.error) return item.error;
  const state = sandboxActionState(item.detail, item.observedAtMs, nowMs);
  const blocked = sandboxActionBlocked(kind, state);
  if (blocked) return blocked;
  if (kind === 'archive' && item.detail?.sandbox.archived_at)
    return 'This sandbox is already removed from inventory.';
  if (kind === 'restore' && !item.detail?.sandbox.archived_at)
    return 'This sandbox is already in inventory.';
  return null;
}

function confirmation(kind: SandboxAction, items: ReviewedSandbox[]): string {
  const sessions = items.reduce((sum, item) => sum + (item.detail?.sandbox.member_count ?? 0), 0);
  const label = ACTIONS.find((action) => action.value === kind)!.label;
  const scope = `${label} for ${items.length} sandboxes shared by ${sessions} member sessions? `;
  if (kind === 'stop')
    return `${scope}This stops each idle container for every member and retains its workspace files. New work is blocked until the host confirms the stop.`;
  if (kind === 'archive')
    return `${scope}Only inventory entries are hidden. Retained containers, workspace files, native sessions, and chat history are kept. Offline entries can return when their host reports or new work resumes.`;
  if (kind === 'restore')
    return `${scope}This makes the selected entries visible in the admin inventory again.`;
  const running = items.some((item) => item.detail?.sandbox.state === 'running');
  const verification = running
    ? 'For entries recorded as running, the server must verify that the exact container is already stopped and idle. This does not stop a running container. '
    : '';
  return (
    scope +
    verification +
    (kind === 'discard'
      ? 'Workspace files and native session history on the host will be permanently destroyed without uploading an S3 archive and cannot be restored. Live native resume will no longer be available. Chat history remains.'
      : 'Workspace files and native session history will be uploaded to S3 and verified before deletion. Live native resume will no longer be available. Chat history remains.')
  );
}

export function BatchSandboxManagement({
  selected,
  nowMs,
  onDenied,
  onChanged,
  onCompleted,
  onBusyChange,
}: {
  selected: SandboxObservation[];
  nowMs: number;
  onDenied: () => void;
  onChanged: () => void;
  onCompleted: (ids: string[]) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [kind, setKind] = useState<SandboxAction>('delete');
  const [review, setReview] = useState<ReviewedSandbox[] | null>(null);
  const [results, setResults] = useState<BatchResult[]>([]);
  const [busy, setBusy] = useState<'review' | 'apply' | null>(null);
  const request = useRef<AbortController | null>(null);
  const selectionKey = JSON.stringify(selected.map(({ sandbox }) => sandbox.id).sort());

  useEffect(() => {
    setReview(null);
  }, [selectionKey, kind]);
  useEffect(() => () => request.current?.abort(), []);

  function updateResult(id: string, status: BatchResult['status'], message: string) {
    setResults((current) =>
      current.map((item) => (item.id === id ? { ...item, status, message } : item)),
    );
  }

  async function reviewSelected() {
    if (request.current || selected.length === 0) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy('review');
    onBusyChange(true);
    setReview(null);
    setResults([]);
    const items: ReviewedSandbox[] = selected.map(({ sandbox }) => ({
      id: sandbox.id,
      title: sandbox.title ?? sandbox.id,
      detail: null,
      observedAtMs: 0,
      error: null,
    }));
    try {
      await runBounded(items, controller.signal, async (item) => {
        try {
          const detail = await getAdminSandbox(item.id, controller.signal);
          if (controller.signal.aborted) return;
          if (detail.sandbox.id !== item.id)
            throw new Error('Sandbox identity changed. Review it again.');
          item.detail = detail;
          item.observedAtMs = performance.now();
        } catch (cause) {
          if (controller.signal.aborted) return;
          if (accessDenied(cause)) {
            controller.abort();
            onDenied();
          } else item.error = getErrorMessage(cause);
        }
      });
      if (!controller.signal.aborted) setReview(items);
    } finally {
      if (!controller.signal.aborted) {
        setBusy(null);
        onBusyChange(false);
      }
      if (request.current === controller) request.current = null;
    }
  }

  async function applyReviewed() {
    if (!review || request.current) return;
    const eligible = review.filter((item) => blockedReason(item, kind, performance.now()) === null);
    if (eligible.length === 0 || !window.confirm(confirmation(kind, eligible))) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy('apply');
    onBusyChange(true);
    setResults(
      review.map((item) => {
        const blocked = blockedReason(item, kind, performance.now());
        return {
          id: item.id,
          title: item.title,
          status: blocked ? 'skipped' : 'queued',
          message: blocked ?? 'Waiting to submit.',
        };
      }),
    );
    const accepted: string[] = [];
    try {
      await runBounded(eligible, controller.signal, async (item) => {
        // A long confirmation or earlier host checks can age the review. Never
        // fetch a new identity and silently retry an administrator's decision.
        const blocked = blockedReason(item, kind, performance.now());
        if (blocked) {
          updateResult(item.id, 'skipped', blocked);
          return;
        }
        try {
          const sandbox = item.detail!.sandbox;
          const state = sandboxActionState(item.detail, item.observedAtMs, performance.now());
          await applySandboxAction(kind, sandbox, controller.signal, state.staleInventoryEntry);
          if (controller.signal.aborted) return;
          accepted.push(item.id);
          updateResult(
            item.id,
            kind === 'stop' || kind === 'delete' || kind === 'discard' ? 'requested' : 'completed',
            kind === 'stop'
              ? 'Stop requested. Waiting for the host.'
              : kind === 'delete' || kind === 'discard'
                ? 'Deletion requested. Waiting for the host.'
                : kind === 'archive'
                  ? 'Removed from inventory.'
                  : 'Restored to inventory.',
          );
        } catch (cause) {
          if (controller.signal.aborted) return;
          if (accessDenied(cause)) {
            controller.abort();
            onDenied();
          } else updateResult(item.id, 'failed', getErrorMessage(cause));
        }
      });
      if (!controller.signal.aborted) {
        setReview(null);
        if (accepted.length > 0) {
          onCompleted(accepted);
          onChanged();
        }
      }
    } finally {
      if (!controller.signal.aborted) {
        setBusy(null);
        onBusyChange(false);
      }
      if (request.current === controller) request.current = null;
    }
  }

  const eligibleCount =
    review?.filter((item) => blockedReason(item, kind, nowMs) === null).length ?? 0;
  const finishedCount = results.filter(
    (item) => item.status === 'completed' || item.status === 'requested',
  ).length;
  return (
    <section aria-label="Batch sandbox operations" className="ui-card mt-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-gray-700 dark:text-gray-200">
          {selected.length} sandboxes selected
        </p>
        <select
          aria-label="Batch sandbox action"
          value={kind}
          disabled={busy !== null}
          className="ui-input min-h-11"
          onChange={(event) => setKind(event.target.value as SandboxAction)}
        >
          {ACTIONS.map(({ value, label }) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={BUTTON}
          disabled={busy !== null || selected.length === 0}
          onClick={() => void reviewSelected()}
        >
          {busy === 'review' ? 'Reviewing sandboxes…' : 'Review selected sandboxes'}
        </button>
        {review && (
          <button
            type="button"
            className={BUTTON}
            disabled={busy !== null || eligibleCount === 0}
            onClick={() => void applyReviewed()}
          >
            {busy === 'apply' ? 'Applying batch…' : `Apply to ${eligibleCount} sandboxes`}
          </button>
        )}
      </div>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Select rows or all visible sandboxes, then review their current details. Each sandbox is
        checked separately; busy or changed sandboxes are skipped or refused.
      </p>
      {review && (
        <ul
          aria-label="Sandbox batch review"
          className="mt-3 space-y-1 text-xs text-gray-500 dark:text-gray-400"
        >
          {review.map((item) => (
            <li key={item.id}>
              <span className="font-medium">{item.title}</span> <code>{item.id}</code>:{' '}
              {blockedReason(item, kind, nowMs) ??
                `Ready · ${item.detail!.sandbox.member_count} member sessions`}
            </li>
          ))}
        </ul>
      )}
      {results.length > 0 && (
        <>
          <p role="status" className="mt-3 text-sm text-gray-700 dark:text-gray-200">
            {busy === 'apply' ? 'Batch in progress.' : 'Batch finished.'} {finishedCount} of{' '}
            {results.length} sandboxes accepted.
          </p>
          <ul
            aria-label="Sandbox batch results"
            className="mt-2 space-y-1 text-xs text-gray-500 dark:text-gray-400"
          >
            {results.map((item) => (
              <li key={item.id}>
                <span className="font-medium">{item.title}</span> <code>{item.id}</code>:{' '}
                {item.status} — {item.message}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
