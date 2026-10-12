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
  preserveConfiguration = false,
}: {
  catalog: ComputeCatalog;
  connection: ComputeConnection;
  onSaved: (connection: ComputeConnection) => void;
  onCancel?: () => void;
  preserveConfiguration?: boolean;
}) {
  const provider = catalog.providers.find((item) => item.id === connection.provider_id);
  const formProvider =
    preserveConfiguration && provider ? { ...provider, connection_fields: [] } : provider;
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

  if (!catalog.secret_store_configured || !formProvider) {
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
          const request: Parameters<typeof saveComputeConnection>[0] = {
            name: draft.name,
            provider_id: formProvider.id,
            ...(draft.secret ? { secret: draft.secret } : {}),
            expected_revision: revision,
          };
          if (!preserveConfiguration) {
            request.configuration = parseFields(formProvider.connection_fields, draft.values);
          }
          const result = await saveComputeConnection(request, connection.id);
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
        <CredentialFields provider={formProvider} value={draft} onChange={setDraft} editing />
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
