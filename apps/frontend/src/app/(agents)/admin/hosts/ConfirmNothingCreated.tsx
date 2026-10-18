'use client';

import { useState } from 'react';

import { confirmComputeOperationAbsent, type ComputeOperation } from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import { buttonClass, ErrorNotice } from './compute-ui';

/**
 * Close an acquisition whose provider result is unknown or never arrived, once
 * an administrator found nothing for it in the provider's portal. Only that
 * portal can tell, so the statement is theirs; the provider is not contacted.
 */
export default function ConfirmNothingCreated({
  operation,
  onConfirmed,
}: {
  operation: ComputeOperation;
  onConfirmed: (operation: ComputeOperation) => void;
}) {
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <form
      className="mt-4 space-y-3 border-t border-gray-200 pt-4 dark:border-white/10"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || !checked) return;
        setBusy(true);
        setError(null);
        try {
          onConfirmed((await confirmComputeOperationAbsent(operation.id)).operation);
        } catch (error) {
          setError(getErrorMessage(error));
        } finally {
          setBusy(false);
        }
      }}
    >
      <h3 className="text-sm font-semibold">Nothing was created?</h3>
      <p className="text-sm">
        If the provider portal shows no experiment for this request, close the request here. It is
        never retried, and the server can then be removed.
        {operation.allowed_actions.includes('resolve') &&
          ' If an experiment does exist, adopt it above instead.'}
      </p>
      <label className="flex items-start gap-3 text-sm leading-6">
        <input
          type="checkbox"
          className="mt-1"
          checked={checked}
          disabled={busy}
          onChange={(event) => setChecked(event.target.checked)}
        />
        I checked the provider portal, and it has no experiment for this request.
      </label>
      <ErrorNotice error={error} />
      <button className={buttonClass} type="submit" disabled={!checked || busy}>
        {busy ? 'Closing request…' : 'Nothing was created'}
      </button>
    </form>
  );
}
