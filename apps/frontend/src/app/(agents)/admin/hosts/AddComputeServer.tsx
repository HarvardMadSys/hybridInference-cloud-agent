'use client';

import { useRef, useState } from 'react';

import {
  createComputeServer,
  getComputeOperations,
  importComputeResource,
  preflightComputeServer,
  type ComputeCatalog,
  type ComputeConnection,
  type ComputeOperation,
  type ServerRequest,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import ComputeConnectionForm from './ComputeConnectionForm';

import {
  buttonClass,
  ErrorNotice,
  Field,
  initialFields,
  inputClass,
  OperationDetails,
  parseFields,
  primaryClass,
  ProviderFields,
} from './compute-ui';

export default function AddComputeServer({
  catalog,
  connections,
  operations,
  onOperation,
  onConnectionSaved,
}: {
  catalog: ComputeCatalog;
  connections: ComputeConnection[];
  operations: ComputeOperation[];
  onOperation: (operation: ComputeOperation, submitted: boolean) => void;
  onConnectionSaved: (connection: ComputeConnection) => void;
}) {
  const [connectionTarget, setConnectionTarget] = useState<'account' | 'ssh' | null>(null);
  const [newConnections, setNewConnections] = useState<ComputeConnection[]>([]);
  const availableConnections = [
    ...newConnections,
    ...connections.filter(
      (connection) => !newConnections.some((saved) => saved.id === connection.id),
    ),
  ];
  const [providerId, setProviderId] = useState(catalog.providers[0]?.id ?? '');
  const provider = catalog.providers.find((item) => item.id === providerId);
  const [mode, setMode] = useState<'attach' | 'provision'>(provider?.modes[0] ?? 'attach');
  const [name, setName] = useState('');
  const [connectionId, setConnectionId] = useState('');
  const [sshConnectionId, setSshConnectionId] = useState('');
  const [deploymentProfile, setDeploymentProfile] = useState(
    catalog.deployment_profiles[0]?.id ?? '',
  );
  const [values, setValues] = useState(() => initialFields(provider?.fields ?? []));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preflightId, setPreflightId] = useState<string | null>(null);
  // Retained after an ambiguous network result; repeated submits use the same intent.
  const requestIds = useRef<Partial<Record<'validate' | 'deploy' | 'import', string>>>({});
  const fields = (provider?.fields ?? []).filter(
    (field) => !field.modes || field.modes.includes(mode),
  );
  const preflight = operations.find((operation) => operation.id === preflightId);
  const changed = () => {
    requestIds.current = {};
    setPreflightId(null);
    setError(null);
  };

  async function submit(action: 'validate' | 'deploy' | 'import') {
    if (busy || !provider) return;
    setBusy(true);
    setError(null);
    const requestId = requestIds.current[action] ?? crypto.randomUUID();
    requestIds.current[action] = requestId;
    try {
      const body: ServerRequest = {
        request_id: requestId,
        name,
        provider_id: provider.id,
        mode,
        connection_id: connectionId,
        ...(provider.requires_ssh_connection ? { ssh_connection_id: sshConnectionId } : {}),
        configuration: parseFields(fields, values),
        deployment_profile: deploymentProfile,
      };
      const send =
        action === 'validate'
          ? preflightComputeServer
          : action === 'import'
            ? importComputeResource
            : createComputeServer;
      const { operation } = await send(body);
      if (action === 'validate') setPreflightId(operation.id);
      onOperation(operation, action !== 'validate');
    } catch (error) {
      // A lost response does not justify creating a second resource. Ask by the
      // stable request ID before allowing a retry with the same payload.
      try {
        const recovered = await getComputeOperations(requestId);
        const operation = recovered.operations.find((item) => item.request_id === requestId);
        if (operation) {
          if (action === 'validate') setPreflightId(operation.id);
          onOperation(operation, action !== 'validate');
          return;
        }
      } catch {
        /* The same request ID remains available for reconciliation. */
      }
      setError(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  if (connectionTarget) {
    return (
      <div className="space-y-5">
        <button type="button" className={buttonClass} onClick={() => setConnectionTarget(null)}>
          ← Back to server setup
        </button>
        <h3 className="text-base font-semibold">
          {connectionTarget === 'ssh' || providerId === 'existing_ssh'
            ? 'Add SSH connection'
            : `Add ${provider?.label ?? ''} connection`}
        </h3>
        <ComputeConnectionForm
          catalog={catalog}
          fixedProviderId={connectionTarget === 'ssh' ? 'existing_ssh' : providerId}
          onSaved={(connection) => {
            setNewConnections((current) => [
              connection,
              ...current.filter((item) => item.id !== connection.id),
            ]);
            if (connectionTarget === 'ssh') setSshConnectionId(connection.id);
            else setConnectionId(connection.id);
            changed();
            setConnectionTarget(null);
            onConnectionSaved(connection);
          }}
        />
      </div>
    );
  }

  return (
    <form
      className="space-y-4"
      onChange={changed}
      onSubmit={(event) => {
        event.preventDefault();
        const action = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value');
        void submit(action === 'deploy' || action === 'import' ? action : 'validate');
      }}
    >
      <fieldset disabled={busy} className="space-y-4">
        <fieldset>
          <legend className="mb-2 text-sm font-medium">Server type</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            {catalog.providers.map((item) => (
              <label key={item.id} className="cursor-pointer">
                <input
                  className="peer sr-only"
                  type="radio"
                  name="provider"
                  value={item.id}
                  checked={providerId === item.id}
                  onChange={() => {
                    setProviderId(item.id);
                    setMode(item.modes.includes('provision') ? 'provision' : 'attach');
                    setValues(initialFields(item.fields));
                    setConnectionId('');
                    setSshConnectionId('');
                  }}
                />
                <span className="block h-full rounded-xl border border-gray-200 p-4 peer-checked:border-crimson peer-checked:bg-crimson/5 peer-focus-visible:ring-2 peer-focus-visible:ring-crimson dark:border-white/15 dark:peer-checked:border-crimson-light">
                  <span className="block text-sm font-medium">{item.label}</span>
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {item.id === 'existing_ssh'
                      ? 'Connect with SSH'
                      : item.modes.includes('provision')
                        ? 'Create or connect a cloud server'
                        : 'Connect a cloud server'}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        {provider && provider.modes.length > 1 && (
          <Field label="Cloud resource">
            <select
              className={inputClass}
              value={mode}
              onChange={(event) => setMode(event.target.value as typeof mode)}
            >
              {provider?.modes.map((item) => (
                <option key={item} value={item}>
                  {item === 'attach' ? 'Connect existing' : 'Create new'}
                </option>
              ))}
            </select>
          </Field>
        )}
        <Field label="Server name">
          <input
            className={inputClass}
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </Field>
        <Field label="Account connection">
          <select
            className={inputClass}
            required
            value={connectionId}
            onChange={(event) => setConnectionId(event.target.value)}
          >
            <option value="">Select connection…</option>
            {availableConnections
              .filter((item) => item.provider_id === providerId)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.status}
                </option>
              ))}
          </select>
        </Field>
        <button
          type="button"
          className={buttonClass}
          onClick={() => setConnectionTarget('account')}
        >
          {providerId === 'existing_ssh' ? 'Add SSH connection' : 'Add account connection'}
        </button>
        {provider?.requires_ssh_connection && (
          <div className="space-y-2">
            <Field label="SSH deployment connection">
              <select
                className={inputClass}
                required
                value={sshConnectionId}
                onChange={(event) => setSshConnectionId(event.target.value)}
              >
                <option value="">Select SSH connection…</option>
                {availableConnections
                  .filter((item) => item.provider_id === 'existing_ssh')
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name} · {item.status}
                    </option>
                  ))}
              </select>
            </Field>
            <button
              type="button"
              className={buttonClass}
              onClick={() => setConnectionTarget('ssh')}
            >
              Add SSH connection
            </button>
          </div>
        )}
        <ProviderFields fields={fields} values={values} onChange={setValues} />
        <details open={!deploymentProfile}>
          <summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-300">
            Deployment settings
          </summary>
          <div className="mt-3 space-y-3">
            <Field label="Agent deployment">
              <select
                className={inputClass}
                required
                value={deploymentProfile}
                onChange={(event) => setDeploymentProfile(event.target.value)}
              >
                <option value="">Select deployment…</option>
                {catalog.deployment_profiles.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </Field>
            {!catalog.deployment_profiles.length && (
              <p>No deployment profiles are configured on the control plane.</p>
            )}
          </div>
        </details>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Installs the agent. Select this server as the global location once it is ready.
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            className={buttonClass}
            type="submit"
            value="validate"
            disabled={!provider || !deploymentProfile}
          >
            Validate configuration
          </button>
          <button
            className={primaryClass}
            type="submit"
            value="deploy"
            disabled={!provider || !deploymentProfile}
          >
            {mode === 'provision' ? 'Provision and deploy' : 'Connect and deploy'}
          </button>
          {mode === 'attach' && provider?.capabilities.import && (
            <button className={buttonClass} type="submit" value="import" formNoValidate>
              Import for observation only
            </button>
          )}
        </div>
      </fieldset>
      {busy && <p role="status">Submitting request…</p>}
      <ErrorNotice error={error} />
      {preflight && <OperationDetails operation={preflight} />}
    </form>
  );
}
