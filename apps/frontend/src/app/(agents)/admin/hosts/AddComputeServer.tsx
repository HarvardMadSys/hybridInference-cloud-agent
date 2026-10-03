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
  onConnection,
}: {
  catalog: ComputeCatalog;
  connections: ComputeConnection[];
  operations: ComputeOperation[];
  onOperation: (operation: ComputeOperation, submitted: boolean) => void;
  onConnection: () => void;
}) {
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
        <Field label="Resource provider">
          <select
            className={inputClass}
            value={providerId}
            onChange={(event) => {
              const next = catalog.providers.find((item) => item.id === event.target.value);
              setProviderId(next?.id ?? '');
              setMode(next?.modes[0] ?? 'attach');
              setValues(initialFields(next?.fields ?? []));
              setConnectionId('');
              setSshConnectionId('');
            }}
          >
            {catalog.providers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Resource source">
          <select
            className={inputClass}
            value={mode}
            onChange={(event) => setMode(event.target.value as typeof mode)}
          >
            {provider?.modes.map((item) => (
              <option key={item} value={item}>
                {item === 'attach' ? 'Connect an existing resource' : 'Provision a new resource'}
              </option>
            ))}
          </select>
        </Field>
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
            {connections
              .filter((item) => item.provider_id === providerId)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.status}
                </option>
              ))}
          </select>
        </Field>
        {provider?.requires_ssh_connection && (
          <Field label="SSH deployment connection">
            <select
              className={inputClass}
              required
              value={sshConnectionId}
              onChange={(event) => setSshConnectionId(event.target.value)}
            >
              <option value="">Select SSH connection…</option>
              {connections
                .filter((item) => item.provider_id === 'existing_ssh')
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.status}
                  </option>
                ))}
            </select>
          </Field>
        )}
        <button type="button" className={buttonClass} onClick={onConnection}>
          Add connection
        </button>
        <ProviderFields fields={fields} values={values} onChange={setValues} />
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
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {mode === 'provision'
            ? 'This requests a new resource and deploys the selected Agent environment.'
            : 'This connects to an existing resource and deploys the selected Agent environment.'}{' '}
          It becomes available after readiness checks. The global sandbox location stays unchanged.
          Automatic renewal starts disabled and can be configured after acquisition.
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
