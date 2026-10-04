'use client';

import { useState } from 'react';

import {
  saveComputeConnection,
  type ComputeCatalog,
  type ComputeConnection,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import { CredentialFields, type CredentialDraft } from './ComputeCredentials';
import { buttonClass, ErrorNotice, parseFields, primaryClass } from './compute-ui';

export default function ComputeConnectionForm({
  catalog,
  connection,
  onSaved,
  onCancel,
}: {
  catalog: ComputeCatalog;
  connection: ComputeConnection;
  onSaved: (connection: ComputeConnection) => void;
  onCancel?: () => void;
}) {
  const provider = catalog.providers.find((item) => item.id === connection.provider_id);
  const [draft, setDraft] = useState<CredentialDraft>({
    selection: connection.id,
    name: connection.name,
    values: Object.fromEntries(
      Object.entries(connection.configuration).map(([key, value]) => [key, String(value)]),
    ),
    secret: '',
  });
  // The revision belongs to the draft, even when inventory polling returns a newer record.
  const [revision] = useState(connection.revision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!catalog.secret_store_configured || !provider) {
    return <p>Credential editing is unavailable.</p>;
  }

  return (
    <form
      className="space-y-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy) return;
        setBusy(true);
        setError(null);
        try {
          const result = await saveComputeConnection(
            {
              name: draft.name,
              provider_id: provider.id,
              configuration: parseFields(provider.connection_fields, draft.values),
              ...(draft.secret ? { secret: draft.secret } : {}),
              expected_revision: revision,
            },
            connection.id,
          );
          setDraft((current) => ({ ...current, secret: '' }));
          onSaved(result.connection);
        } catch (error) {
          setError(getErrorMessage(error));
        } finally {
          setBusy(false);
        }
      }}
    >
      <fieldset disabled={busy} className="space-y-4">
        <CredentialFields provider={provider} value={draft} onChange={setDraft} editing />
        <ErrorNotice error={error} />
        <div className="flex gap-2">
          <button className={primaryClass} disabled={busy} type="submit">
            {busy ? 'Saving…' : 'Save changes'}
          </button>
          {onCancel && (
            <button className={buttonClass} type="button" onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
      </fieldset>
    </form>
  );
}
