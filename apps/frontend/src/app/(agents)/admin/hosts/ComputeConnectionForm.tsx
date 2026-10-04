'use client';

import { useState } from 'react';

import {
  saveComputeConnection,
  type ComputeCatalog,
  type ComputeConnection,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  ErrorNotice,
  Field,
  initialFields,
  inputClass,
  parseFields,
  primaryClass,
  ProviderFields,
} from './compute-ui';

export default function ComputeConnectionForm({
  catalog,
  connection,
  fixedProviderId,
  onSaved,
}: {
  catalog: ComputeCatalog;
  connection?: ComputeConnection;
  fixedProviderId?: string;
  onSaved: (connection: ComputeConnection) => void;
}) {
  const [providerId, setProviderId] = useState(
    connection?.provider_id ?? fixedProviderId ?? catalog.providers[0]?.id,
  );
  const provider = catalog.providers.find((item) => item.id === providerId);
  const [name, setName] = useState(connection?.name ?? '');
  const [values, setValues] = useState(() => {
    if (!connection) return initialFields(provider?.connection_fields ?? []);
    return Object.fromEntries(
      Object.entries(connection.configuration).map(([key, value]) => [key, String(value)]),
    );
  });
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!catalog.secret_store_configured) {
    return <p>Connection management is unavailable until the server secret store is configured.</p>;
  }

  return (
    <form
      className="space-y-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || !provider) return;
        setBusy(true);
        setError(null);
        try {
          const result = await saveComputeConnection(
            {
              name,
              provider_id: provider.id,
              configuration: parseFields(provider.connection_fields, values),
              ...(secret ? { secret } : {}),
              ...(connection ? { expected_revision: connection.revision } : {}),
            },
            connection?.id,
          );
          setSecret('');
          onSaved(result.connection);
        } catch (error) {
          // No form values or secrets are logged, persisted, or included in diagnostics.
          setError(getErrorMessage(error));
        } finally {
          setBusy(false);
        }
      }}
    >
      <Field label="Connection name">
        <input
          className={inputClass}
          value={name}
          required
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <Field label="Connection provider">
        <select
          className={inputClass}
          value={providerId}
          disabled={Boolean(connection || fixedProviderId)}
          onChange={(event) => {
            const next = catalog.providers.find((item) => item.id === event.target.value);
            setProviderId(next?.id ?? '');
            setValues(initialFields(next?.connection_fields ?? []));
            setSecret('');
          }}
        >
          {catalog.providers.map((provider) => (
            <option value={provider.id} key={provider.id}>
              {provider.label}
            </option>
          ))}
        </select>
      </Field>
      <ProviderFields
        fields={provider?.connection_fields ?? []}
        values={values}
        onChange={setValues}
      />
      <Field label={provider?.secret_label ?? 'Credential'}>
        <textarea
          className={inputClass}
          rows={4}
          required={!connection}
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          placeholder={connection ? 'Leave blank to keep the current credential' : undefined}
        />
      </Field>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        This credential cannot be viewed after saving.
      </p>
      <ErrorNotice error={error} />
      <button className={primaryClass} disabled={busy || !provider} type="submit">
        {busy ? 'Saving…' : connection ? 'Save connection changes' : 'Save connection'}
      </button>
    </form>
  );
}
