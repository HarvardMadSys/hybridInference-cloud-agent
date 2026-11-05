'use client';

import { useState } from 'react';

import { forgetAgentRunnerHost } from '@/lib/api/admin';
import { removeComputeServer, type ComputeAllocation } from '@/lib/api/compute';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import { buttonClass, dangerClass, ErrorNotice } from './compute-ui';

// Refusals whose way out is elsewhere on this server: an operation to settle
// in its activity, or a resource to release first.
const SETTLE_OPERATION = new Set(['external_operation_unresolved']);
const RELEASE_FIRST = new Set(['resource_release_required', 'autoscaling_member_active']);

export default function RemoveComputeServer({
  name,
  allocation,
  hosts = [],
  onRemoved,
  onPartialRemoval,
  onCancel,
  onShowActivity,
  release,
}: {
  name: string;
  allocation?: ComputeAllocation;
  hosts?: string[];
  onRemoved: () => void;
  onPartialRemoval: () => void;
  onCancel: () => void;
  onShowActivity?: () => void;
  release?: { label: string; onSelect: () => void };
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [remainingHosts, setRemainingHosts] = useState([...new Set(hosts)]);

  async function remove() {
    if (busy || !confirmed) return;
    setBusy(true);
    setError(null);
    setRefusal(null);
    let removedHosts = 0;
    try {
      if (allocation) {
        await removeComputeServer(allocation.id, allocation.revision);
      } else {
        // Keep successful deletions out of a retry when a grouped enrollment
        // still has dependencies. Each Host endpoint enforces its own guards.
        for (const host of remainingHosts) {
          await forgetAgentRunnerHost(host);
          removedHosts += 1;
          setRemainingHosts((current) => current.filter((item) => item !== host));
        }
      }
      onRemoved();
    } catch (error) {
      if (removedHosts > 0) onPartialRemoval();
      setError(
        `${removedHosts > 0 ? `${removedHosts} Host record(s) removed. Remaining records: ` : ''}${getErrorMessage(error)}`,
      );
      setRefusal(error instanceof APIError ? error.code : null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        void remove();
      }}
    >
      <h3 className="text-base font-semibold">Remove server</h3>
      <p className="text-sm leading-6">
        Permanently remove <strong>{name}</strong> from the inventory and delete its{' '}
        {allocation ? 'resource, node and Host records' : 'Host records'}, server settings,
        observations and activity history. This cannot be undone.
      </p>
      <p className="text-sm leading-6">
        {allocation
          ? allocation.provider_id === 'existing_ssh'
            ? 'Disable or disconnect this server first. Its machine and files remain intact.'
            : 'Release this cloud resource first, unless acquisition failed before a resource was created.'
          : 'Stop the host agent first. Its machine and files remain intact.'}{' '}
        Delete retained sandboxes and clear running work and placement references before removing
        the records.
      </p>
      <p className="text-sm text-muted">
        Shared credentials, server setup, user conversations and run history remain available.
      </p>
      <label className="flex items-start gap-3 rounded-xl border border-status-error/20 bg-status-error/5 p-4 text-sm leading-6">
        <input
          type="checkbox"
          className="mt-1"
          checked={confirmed}
          disabled={busy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        I confirm permanent removal of this server and its management records.
      </label>
      <ErrorNotice error={error} />
      {refusal !== null && SETTLE_OPERATION.has(refusal) && onShowActivity && (
        <button type="button" className={buttonClass} onClick={onShowActivity}>
          Open activity
        </button>
      )}
      {refusal !== null && RELEASE_FIRST.has(refusal) && release && (
        <button type="button" className={buttonClass} onClick={release.onSelect}>
          {release.label}
        </button>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={!confirmed || busy} className={dangerClass}>
          {busy ? 'Removing server…' : 'Permanently remove server'}
        </button>
        <button type="button" className={buttonClass} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
