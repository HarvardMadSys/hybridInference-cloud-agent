'use client';

import { useRef, useState } from 'react';

import {
  createComputeServer,
  getComputeOperations,
  importComputeResource,
  preflightComputeServer,
  saveComputeConnection,
  type ComputeCatalog,
  type ComputeConnection,
  type ComputeOperation,
  type ComputeProvider,
  type ServerRequest,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  CredentialPicker,
  initialCredential,
  NEW_CREDENTIAL,
  type CredentialDraft,
} from './ComputeCredentials';

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
  const [account, setAccount] = useState(() => initialCredential(provider, connections));
  const sshProvider = catalog.providers.find((item) => item.id === 'existing_ssh');
  const [ssh, setSsh] = useState(() => initialCredential(sshProvider, connections));
  const deploymentConfigured = catalog.deployment_profiles.length > 0;
  const [values, setValues] = useState(() => initialFields(provider?.fields ?? []));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preflightId, setPreflightId] = useState<string | null>(null);
  // Retained after an ambiguous network result; repeated submits use the same intent.
  const requestIds = useRef<Partial<Record<'validate' | 'deploy' | 'import', string>>>({});
  const fields = (provider?.fields ?? []).filter(
    (field) => !field.modes || field.modes.includes(mode),
  );
  const mainFields = fields.filter((field) => !field.advanced || field.required);
  const advancedFields = fields.filter((field) => field.advanced && !field.required);
  const preflight = operations.find((operation) => operation.id === preflightId);
  const changed = () => {
    requestIds.current = {};
    setPreflightId(null);
    setError(null);
  };

  async function saveCredential(
    draft: CredentialDraft,
    owner: ComputeProvider,
    update: (draft: CredentialDraft) => void,
  ): Promise<string> {
    if (draft.selection !== NEW_CREDENTIAL) {
      if (!draft.selection)
        throw new Error(`Select a ${owner.id === 'existing_ssh' ? 'SSH key' : 'cloud account'}.`);
      return draft.selection;
    }
    const { connection } = await saveComputeConnection({
      name:
        draft.name.trim() ||
        `${name.trim() || 'Server'} ${owner.id === 'existing_ssh' ? 'key' : 'account'}`,
      provider_id: owner.id,
      configuration: parseFields(owner.connection_fields, draft.values),
      secret: draft.secret,
    });
    // Keep each successful save across partial failures; never send a secret with a server request.
    update({ ...draft, selection: connection.id, secret: '' });
    setNewConnections((current) => [
      connection,
      ...current.filter((item) => item.id !== connection.id),
    ]);
    onConnectionSaved(connection);
    return connection.id;
  }

  async function submit(action: 'validate' | 'deploy' | 'import') {
    if (busy || !provider) return;
    setBusy(true);
    setError(null);
    const requestId = requestIds.current[action] ?? crypto.randomUUID();
    requestIds.current[action] = requestId;
    let submitted = false;
    try {
      if (action !== 'import' && !deploymentConfigured) {
        throw new Error('Server setup is not configured. Contact your administrator.');
      }
      const configuration = parseFields(fields, values);
      if (provider.requires_ssh_connection && action !== 'import' && !sshProvider) {
        throw new Error('SSH access is not configured.');
      }
      const connectionId = await saveCredential(account, provider, setAccount);
      const sshConnectionId =
        provider.requires_ssh_connection && action !== 'import'
          ? await saveCredential(ssh, sshProvider!, setSsh)
          : undefined;
      const body: ServerRequest = {
        request_id: requestId,
        name,
        provider_id: provider.id,
        mode,
        connection_id: connectionId,
        ...(sshConnectionId ? { ssh_connection_id: sshConnectionId } : {}),
        configuration,
      };
      const send =
        action === 'validate'
          ? preflightComputeServer
          : action === 'import'
            ? importComputeResource
            : createComputeServer;
      submitted = true;
      const { operation } = await send(body);
      if (action === 'validate') setPreflightId(operation.id);
      onOperation(operation, action !== 'validate');
    } catch (error) {
      // A lost response does not justify creating a second resource. Ask by the
      // stable request ID before allowing a retry with the same payload.
      try {
        const recovered = submitted ? await getComputeOperations(requestId) : { operations: [] };
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
        void submit(action === 'validate' || action === 'import' ? action : 'deploy');
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
                    setAccount(initialCredential(item, availableConnections));
                    setSsh(initialCredential(sshProvider, availableConnections));
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
        <ProviderFields fields={mainFields} values={values} onChange={setValues} />
        {advancedFields.length > 0 && (
          <details key={`${providerId}-${mode}`}>
            <summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-300">
              Advanced options
            </summary>
            <div className="mt-3 space-y-3">
              <ProviderFields fields={advancedFields} values={values} onChange={setValues} />
            </div>
          </details>
        )}
        {provider && (
          <CredentialPicker
            provider={provider}
            connections={availableConnections}
            value={account}
            onChange={setAccount}
            serverName={name}
          />
        )}
        {provider?.requires_ssh_connection && sshProvider && (
          <CredentialPicker
            provider={sshProvider}
            connections={availableConnections}
            value={ssh}
            onChange={setSsh}
            serverName={name}
          />
        )}
        {!deploymentConfigured && (
          <ErrorNotice error="Server setup is not configured. Contact your administrator." />
        )}
        <p className="text-xs text-gray-500 dark:text-gray-400">
          The agent will be installed on this server. You can use it for new sandboxes once it is
          ready.
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            className={primaryClass}
            type="submit"
            value="deploy"
            disabled={!provider || !deploymentConfigured}
          >
            {mode === 'provision' ? 'Create server' : 'Add server'}
          </button>
          <button
            className={buttonClass}
            type="submit"
            value="validate"
            disabled={!provider || !deploymentConfigured}
          >
            Validate configuration
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
