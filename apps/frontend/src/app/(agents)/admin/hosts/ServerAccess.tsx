'use client';

import { useState } from 'react';

import {
  validateComputeConnection,
  type ComputeAllocation,
  type ComputeCatalog,
  type ComputeConnection,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import ComputeConnectionForm from './ComputeConnectionForm';
import { credentialLabel } from './ComputeCredentials';
import { buttonClass, ErrorNotice, Reasons } from './compute-ui';

export default function ServerAccess({
  allocation,
  allocations,
  catalog,
  connections,
  onSaved,
}: {
  allocation: ComputeAllocation;
  allocations: ComputeAllocation[];
  catalog: ComputeCatalog;
  connections: ComputeConnection[];
  onSaved: (connection: ComputeConnection) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const ids = [
    ...new Set([allocation.connection_id, allocation.ssh_connection_id].filter(Boolean)),
  ];
  const saved = ids.flatMap((id) => connections.filter((connection) => connection.id === id));
  const editor = saved.find((connection) => connection.id === editing);
  const sharing = allocations.filter(
    (server) =>
      !['terminated', 'absent'].includes(server.state) &&
      [server.connection_id, server.ssh_connection_id].includes(editing),
  ).length;

  return (
    <section className="space-y-4" aria-label="Server access">
      <h3 className="text-sm font-semibold">Accounts & SSH keys</h3>
      {editor ? (
        <>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Editing {editor.name}
            {sharing > 1 ? `. Used by ${sharing} servers; changes apply to all of them.` : '.'}
          </p>
          <ComputeConnectionForm
            key={editor.id}
            catalog={catalog}
            connection={editor}
            onSaved={(connection) => {
              onSaved(connection);
              setEditing(null);
              setNotice('Access settings saved.');
            }}
            onCancel={() => setEditing(null)}
          />
        </>
      ) : (
        <div className="divide-y divide-gray-100 rounded-xl border border-gray-200 dark:divide-white/10 dark:border-white/10">
          {saved.map((connection) => (
            <div key={connection.id} className="space-y-2 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {credentialLabel(
                      connection.provider_id,
                      catalog.providers.find((provider) => provider.id === connection.provider_id)
                        ?.label,
                    )}
                  </p>
                  <p className="mt-1 break-words text-sm font-medium">{connection.name}</p>
                </div>
                <div className="flex gap-2">
                  <button
                    className={buttonClass}
                    type="button"
                    disabled={!catalog.secret_store_configured || busy !== null}
                    onClick={() => {
                      setEditing(connection.id);
                      setError(null);
                      setNotice(null);
                    }}
                  >
                    Edit {connection.provider_id === 'existing_ssh' ? 'SSH key' : 'account'}
                  </button>
                  <button
                    className={buttonClass}
                    type="button"
                    disabled={!catalog.secret_store_configured || busy !== null}
                    onClick={async () => {
                      setBusy(connection.id);
                      setError(null);
                      setNotice(null);
                      try {
                        const result = await validateComputeConnection(connection.id);
                        onSaved(result.connection);
                        setNotice(
                          result.connection.status === 'verified'
                            ? `${connection.name} verified.`
                            : 'Check the access details below.',
                        );
                      } catch (error) {
                        setError(getErrorMessage(error));
                      } finally {
                        setBusy(null);
                      }
                    }}
                  >
                    {busy === connection.id ? 'Checking…' : 'Check'}
                  </button>
                </div>
              </div>
              <Reasons reasons={connection.blocked_reasons} />
            </div>
          ))}
          {saved.length < ids.length && (
            <p className="p-4 text-sm text-gray-500 dark:text-gray-400">
              Some access settings could not be loaded.
            </p>
          )}
          {ids.length === 0 && (
            <p className="p-4 text-sm text-gray-500 dark:text-gray-400">
              No saved access settings for this server.
            </p>
          )}
        </div>
      )}
      {notice && (
        <p role="status" className="text-sm text-gray-600 dark:text-gray-300">
          {notice}
        </p>
      )}
      <ErrorNotice error={error} />
    </section>
  );
}
